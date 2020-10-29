import os
import time
import subprocess
import requests
import pandas as pd
from time import sleep
import glob
import signal
from shutil import copyfile
import random


#PARAMS
SLEEP_SINGLE = 0.1
SLEEP_BATCH = 10
SLEEP_RESTART = 60
BATCH_SIZE = 5000  # store results every batch_size trips and give a break to a server
RESTART_EVERY = 30
QUERY_MODES = "TRANSIT,WALK"



def make_query(row):
    """
    creates OTP query from the single row in requests
    """
    query = dict()
    query['fromPlace'] = "{},{}".format(row.origin_y, row.origin_x)
    query['toPlace'] = "{},{}".format(row.destination_y, row.destination_x)
    hour, minute = row.treq.hour, row.treq.minute
    if int(hour) < 12:
        ampm = 'am'
    else:
        hour = int(hour) - 12
        ampm = 'pm'
    query['time'] = "{:02d}:{}{}".format(int(hour), minute, ampm)

    query['date'] = "{}-{}-{}".format(row.treq.month, row.treq.day, row.treq.year)

    query['mode'] = QUERY_MODES
    query['maxWalkDistance'] = 2000
    query['arriveBy'] = 'false'
    return query


def parse_OTP_response(response):
    '''
    parses OTP response (.json) and populates dictionary of PT trip attributes
    :param response: OTP server response
    :param store_modes: do we store information on modes
    :return: one row of resulting database
    '''
    if 'plan' in response.keys():
        plan = response['plan']
        modes = list()
        shortest = 0
        duration = 99999
        # find the shortest
        for it in plan['itineraries']:
            dur = it['duration']
            if dur < duration:
                duration = dur
                shortest = it

        for leg in shortest['legs']:
            modes.append([leg['mode'], int(leg['duration']), int(leg['distance'])])

        ret = {'success': True,
               "n_itineraries": len(plan['itineraries']),
               'duration': shortest['duration'],
               'walkDistance': shortest['walkDistance'],
               'transfers': shortest['transfers'],
               'transitTime': shortest['transitTime'],
               'waitingTime': shortest['waitingTime'],
               'modes': modes}
    else:
        ret = {'success': False}
    # ret_str = """Trip from ({:.4f},{:.4f}) to ({:.4f},{:.4f}) at {}.
    # \n{} connections found. \nBest one is {:.0f}min ({:.0f}m walk, {} transfer(s), wait time {:.2f}min)""".format(ret)
    return ret


def get_latest(path):
    """
    returns the id of the last succesffully queried requests
    useful to warm restart after server is down
    :param path:
    :return:
    """
    try:
        df = pd.read_csv(path, index_col=[0])  # load the csv
        return int(df[df.success].index.max() - 1)  # last one with success
    except:
        return 0


def test_server(dataset_path):
    batch_df = pd.read_csv(dataset_path, index_col=[0]).sample(5)  # load the csv
    queries = batch_df.apply(make_query, axis=1)  # make OTP query for each trip in dataset
    print('test server on 5 sample trips')

    ret_dict = list()
    for id, query in queries.iteritems():
        try:
            r = requests.get("http://localhost:8080/otp/routers/default/plan", params=query)
            ret = parse_OTP_response(r.json())
            print(query, ret)
        except Exception as err:
            print(f"Exception occured: {err}")
            ret = {'success': False}
            pass
        ret['id'] = id
        print(id, ret['success'])
        ret_dict.append(ret)


def query_dataset(PATH, OUTPATH, BATCHES_PATH = None):
    df = pd.read_csv(PATH, index_col=[0]).sort_index()  # load the csv
    df.treq = pd.to_datetime(df.treq)
    first_index = get_latest(OUTPATH)
    if first_index > 0:
        df = df.loc[first_index:]
    print('trips processed so far: ', first_index)
    print('trips to process ', df.shape[0])

    # loop over batches
    for batch in range((max(BATCH_SIZE, df.shape[0]) // BATCH_SIZE)):
        batch_df = df.iloc[BATCH_SIZE * batch:BATCH_SIZE * (batch + 1)]  # process this batch only
        queries = batch_df.apply(make_query, axis=1)  # make OTP query for each trip in dataset

        ret_list = list()
        for id, query in queries.iteritems():
            try:
                r = requests.get("http://localhost:8080/otp/routers/default/plan", params=query)
                ret = parse_OTP_response(r.json())
            except Exception as err:
                print(f"Exception occured: {err}")
                ret = {'success': False}
                pass
            ret['id'] = id
            print(id, ret['success'])
            if not ret['success']:
                print('Not found for: ', query)
            ret_list.append(ret)
            sleep(SLEEP_SINGLE)  # Time in seconds

        if len(ret_list) > 0:
            batch_out = pd.DataFrame(ret_list).set_index('id').sort_index()

            batch_name = '{}_{}.csv'.format(batch_df.index.min(), batch_df.index.max())
            batch_out.to_csv(os.path.join(BATCHES_PATH, batch_name))

            print('batch {} saved with {} out of {} trips success'.format(batch,
                                                                          batch_out[batch_out.success].shape[0],
                                                                          BATCH_SIZE))
            sleep(SLEEP_BATCH)  # Time in seconds
        if batch >= RESTART_EVERY:
            print("scheduled server restart")
            return -1
    return 1


def merge_batches(path, out_path, remove=True):
    """
    walks through directory and merges all csv files into one
    :param path:
    :param out_path:
    :param remove:
    :return: none
    """

    all_files = glob.glob(path + "/*.csv")
    df = pd.concat((pd.read_csv(f) for f in all_files), sort=False).set_index('id')
    df.to_csv(os.path.join(out_path))
    if remove:
        for f in all_files:
            os.remove(f)


def main(start_server = True):
    OTP_PATH = "otp-1.4.0-shaded.jar" # path to OTP executable
    CITY_PATH = "data"  # folder with osm and gtfs files

    PATH = 'georequests.csv'  # path with trips to query
    OUTPATH = PATH[:-4] + "_PT.csv"

    BATCHES_PATH = 'batches'  # path with trips to query
    if not os.path.exists(BATCHES_PATH):
        os.makedirs(BATCHES_PATH)


    print('starting server')
    #run java server
    if start_server:
        with open("stdout.txt", "wb") as out, open("stderr.txt", "wb") as err:
            p = subprocess.Popen(['java', '-Xmx12G', '-jar', OTP_PATH, '--build', CITY_PATH, '--inMemory'],
                                 stdout=out, stderr=err)

        while True:
            if 'Grizzly server running' in open('stdout.txt').read():
                print('server_running')
                break
            time.sleep(1)

    flag = query_dataset(PATH, OUTPATH, BATCHES_PATH)
    if flag < 0:
        print('terminating server')
        time.sleep(SLEEP_RESTART)
    else:
        print('flag positive')
    if start_server:
        p.terminate()
    print('merging processed batches')
    merge_batches(path=BATCHES_PATH, out_path=OUTPATH, remove=False)


if __name__ == "__main__":
    main(start_server=False)
    # for CITY in ['Warsaw', 'Amsterdam', 'Houston', 'NYC', 'Stockholm', 'DC', 'DC_BUS']:
    #     if CITY == 'DC_BUS':
    #         QUERY_MODES = 'BUS,WALK'
    #     else:
    #         QUERY_MODES = "TRANSIT,WALK"
    #
    #     print('\n\n========= {} =========\n\n'.format(CITY))
    #     main(CITY='Warsaw')
    # for CITY in ['Stockholm']:
    #    print('========= {} ========='.format(CITY))
    # CITY = 'DC'
    # test_me(CITY = CITY)
    # main(CITY= CITY)
    # QUERY_MODES = 'BUS,WALK'
    # CITY = 'DC_BUS'
    # main(CITY = CITY)
    # test_restart(CITY = 'Warsaw')

from IPy import IP
import re
import hbase_client
import uuid
import time
import tldextract
from datetime import datetime
from location.geo_ip import GeoIPUtil
from metadata import metadata

log_file_path = '/home/ubuntu/datasets/nasa_log_jul'  # path to extract the log file
regex = "(.*) - - \[(.*)\] \"([A-Z]+) (.*)\" ([0-9]{3}) (-|[0-9]+)"  # regex to get relevant information from a log entry
pattern = re.compile(regex)

FAMILIES = ['log_info','loca_info']  # column family names for server_logs_table
TABLE = 'server_logs'  # table
LOG_COLS =  ["log_info :host", "log_info:server_ts", "log_info:type", "log_info:url", "log_info:status", "log_info:bytes"] #columns to insert into server_logs
# hbase_client.truncate_table(TABLE, FAMILIES)

tgt_time_fmt = "%Y-%m-%d %H:%M:%S"  # target date format
src_time_fmt = "%d/%b/%Y:%H:%M:%S %z"  # source date format


denied_requests = [str(a) for a in range(400,500)]
start_time = "1995-07-01 00:00:00"  # start time for server log entries
meta = metadata()
meta.initialize_timer(start_time=start_time)
window = meta.next_bound()
lower_b = time.mktime(time.strptime(start_time, tgt_time_fmt))
upper_b = next(window)


geo_ip = GeoIPUtil()
BATCH_LIMIT = 100
count = 1
batch_index = 1
fail_batch_count = 0


"""
Metadata operations for inserting one row of metadata
Input parameters:   count: number of requests in the window, 
                    lower_b: Start time for window,
                    upper_b: End time for window, 
                    num_hosts: number of unique IPs/domain names requesting, 
                    denied_reqs: Number of denied requests
"""


def insert_metadata(count, lower_b, upper_b, num_hosts, denied_reqs):
    d_meta = {}
    d_meta['metainfo:count'] = str(count)
    d_meta['metainfo:denied'] = str(denied_reqs)
    d_meta['metainfo:hosts'] = str(num_hosts)
    cal = datetime.utcfromtimestamp(lower_b)
    d_meta['calendar:day'] = str(cal.day)
    d_meta['calendar:month'] = str(cal.month)
    d_meta['calendar:hour'] = str(cal.hour)
    d_meta['calendar:dayofweek'] = str(cal.weekday())
    d_meta['calendar:date'] = str(cal)
    #if count == 0:
    #    print("Corner case found!", str(cal))
    #else:
    #   print(str(count), " for ", str(cal))
    meta.add_row(str(upper_b), d_meta)



"""
Metadata operations after each row is read
Input parameters:   meta: metadata object
                    host: requesting host
                    status: response sent to host
"""


def inc_metadata(meta, host, status):
    meta.inc_count()
    meta.add_host(host)
    if status in denied_requests:
        meta.inc_denied()



"""
Metadata operations after end of each window
Input parameters:   meta: metadata object
"""


def reset_metadata(meta):
    meta.reset_count()
    meta.reset_denied()
    meta.reset_hosts()


"""
Function to validate an IP address
Input parameters:   host: hostname of incoming request
Returns:    True if host is an IP
            False otherwise
"""


def is_IP(host):
    try:
        IP(host)
        return True
    except ValueError:
        return False


"""
Removes the protocol part in the requested URL.
Input parameters:   url: URL requested by the host
Returns:    url: URL requested by the host, without HTTP details
"""


def transform_URL(url):
    url = re.sub("HTTP.*$", '', url) #replacing the suffix HTTP.*
    return url


"""
Removes the protocol part in the requested URL.
Input parameters:   url: URL requested by the host
Returns:    url: URL requested by the host, without HTTP details
"""


def transform_server_ts(server_ts):
    server_ts = datetime.strptime(server_ts, src_time_fmt)
    server_ts = server_ts.strftime(tgt_time_fmt)
    return server_ts


"""
Function to fetch the domain extension - like edu, com, ac.au, ....
Input parameters:   name: domain name of the host
Returns:    ext: domain extension object
"""


def get_domain_ext(name):
    obj = tldextract.extract(name)
    return obj.suffix


"""
Removes the protocol part in the requested URL.
Input parameters:   url: URL requested by the host
Returns:    url: URL requested by the host, without HTTP details
"""


def transform_loca_info(loca_info):
    LOCA_COL_FAMILY = 'loca_info'
    result = {}
    for k,v in loca_info.items():
        if v is not None:
            new_key = LOCA_COL_FAMILY + ':' + k
            result[new_key] = str(v)
    return result


# Extract information line by line
with open(log_file_path, "r", encoding='ISO-8859-1') as file:
    start = time.time()
    batch = {}  # empty batch

    for line in file:
        # timer
        stage0 = time.time()

        match = pattern.match(line) # try to see if log entry matches the regex
        if match is None:
            continue

        try:
            log_info = {}
            # Transform
            for i in range(len(LOG_COLS)):
                log_info[LOG_COLS[i]] = match.group(i+1)

            #####
            # OPERATION ON BYTES TRANSFERRED
            #####

            status = log_info["log_info:status"] # BYTES

            if log_info["log_info:bytes"] == '-':
                log_info["log_info:bytes"] = '0'

            #####
            # OPERATION ON URL
            #####

            url = transform_URL(log_info['log_info:url'])
            log_info['log_info:url'] = url

            #####
            # OPERATION ON timestamp
            #####

            server_ts = transform_server_ts(log_info['log_info:server_ts'])
            log_info['log_info:server_ts'] = server_ts

            stage1 = time.time()

            #####
            # OPERATION ON HOSTNAME
            #####

            host = log_info['log_info:host']
            if is_IP(host):
                log_info['log_info:is_ip'] = '1'
                loca_info = geo_ip.get_loc_by_ip(host)
            else:
                ext = get_domain_ext(host)
                log_info['log_info:domain_ext'] = ext
                loca_info = geo_ip.get_country_by_domain(host)

            result = log_info.copy()
            if loca_info is not None:
                loca_info = transform_loca_info(loca_info)
                result.update(loca_info)

            stage2 = time.time()
            ROW_ID = str(time.time())   # str(uuid.uuid1())
            batch[ROW_ID] = result  # add entry to batch

            # batch size check
            if len(batch) == BATCH_LIMIT:
                try:
                    print('Batch Index:', batch_index)
                    hbase_client.insert_batch(TABLE, batch)
                except Exception as e:
                    print (e)
                    fail_batch_count += 1
                batch.clear()
                batch_index += 1
                
            stage3 = time.time()
            # print("{},{},{}".format(stage1 - stage0, stage2 - stage1, stage3 - stage2))
        
            curr_ts = time.mktime(time.strptime(server_ts, tgt_time_fmt))
            # print(str(curr_ts), str(lower_b), str(upper_b))
            if lower_b < curr_ts <= upper_b:
                inc_metadata(meta, host, status)
                # increment count of requests

            # crosssing the boundry
            elif curr_ts > upper_b:
                insert_metadata(meta.count, lower_b, upper_b, meta.get_num_hosts(), meta.denied)
                reset_metadata(meta)
                lower_b = upper_b
                upper_b = next(window)

                while curr_ts > upper_b:
                    insert_metadata(0, lower_b, upper_b,0,0)
                    lower_b = upper_b
                    upper_b = next(window)

                inc_metadata(meta, host, status)

            count += 1
        except Exception as e:
            print('Exception while processing log: ', line)
            print(e)
    
    insert_metadata(meta.count, lower_b, upper_b,  meta.get_num_hosts(), meta.denied)

    if len(batch) > 0:
        try:
            hbase_client.insert_batch(TABLE, batch)
        except Exception as e:
            print (e)
            fail_batch_count += 1

    end = time.time()
    print('Total Time(sec):', end - start)
    print('Total entries processed:', count)
    print('Total batches failed:', fail_batch_count)

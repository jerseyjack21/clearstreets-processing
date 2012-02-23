import json
import sqlite3
import time
import datetime
import urllib2
from time import sleep
from collections import defaultdict, deque
from math import exp
import numpy
from scipy.optimize import fsolve

# Helper function for dealing with time stamps
time_format = "%a %b %d %H:%M:%S %Z %Y"
def formatTime(s, time_format) :
    return datetime.datetime(*time.strptime(s, time_format)[:6])


# Rate of change estimator for irregularly sampled data
# we will want to solve for x
#
# Cho and Garcia Molina, 2003, Estimating Frequency of Change
# http://dl.acm.org/citation.cfm?id=857170
def irregularCGM(intervals, Z) :
    def iCGM(x) :
        L = 0
        K = 0
        for i, z in enumerate(Z) :
            if z == 1 :
                L = L + intervals[i]/(exp(x*intervals[i])-1)
            else :
                K = K + intervals[i]
        return L - K
    return iCGM


# Set up DB
con = sqlite3.connect("plow.db")
cur = con.cursor()

cur.execute("""
CREATE TABLE IF NOT EXISTS route_points
(object_id INTEGER,
 posting_time DATETIME,
 X DOUBLE,
 Y DOUBLE,
 UNIQUE(object_id, posting_time) ON CONFLICT REPLACE)""")

cur.execute("""
CREATE TABLE IF NOT EXISTS assets
(object_id INTEGER,
 asset_name TEXT,
 asset_type TEXT,
 PRIMARY KEY(object_id) ON CONFLICT REPLACE)""")

# The feed for City Of Chicago's Plow Data
gps_data_url = "https://gisapps.cityofchicago.org/ArcGISRest/services/ExternalApps/operational/MapServer/38/query?where=POSTING_TIME+>+SYSDATE-30/24/60+&returnGeometry=true&outSR=4326&outFields=ADDRESS,POSTING_TIME,ASSET_NAME,ASSET_TYPE,OBJECTID&f=pjson"


# We'll use these variables to keep track of whether we observe a new
# plow position. 30 observations should be sufficient for a reasonable
# estimate.
previous_posting_time = datetime.datetime(1,1,1)
last_posting_time = datetime.datetime(1,1,1)
update_history = defaultdict( lambda: deque([], 30))
intervals = deque([],30)

# We want adjust our sampling intervals depending upon our estimated
# rate of updates for the plows
sampling_frequency = 10

# Even if we are unable to get any data, we need to keep track of the
# time we spent on that attempt
fault_sleep = 60
faults = 0

while True:
    query = urllib2.Request(gps_data_url)

    # Try to handle anything besides a well-formed json response
    try:
        response = urllib2.urlopen(query).read()
    except Exception as e :
        print e
        sleep(fault_sleep)
        faults += 1
        continue
    if "Sorry, servers are currently down" in response:
        print "Sorry, servers are currently down"
        sleep(fault_sleep)
        faults += 1
        continue

    read_data = json.loads(response)

    updates = 0
    for route_point in read_data['features'] :
        
        (object_id,
         asset_name,
         asset_type,
         posting_time,
         x,
         y) = (route_point['attributes']['OBJECTID'],
               route_point['attributes']['ASSET_NAME'],
               route_point['attributes']['ASSET_TYPE'],
               route_point['attributes']['POSTING_TIME'],
               route_point['geometry']['x'],
               route_point['geometry']['y'])

        posting_time = formatTime(posting_time, time_format)

        # Insert Data into DB
        cur.execute("""insert into route_points (object_id, posting_time, X, Y)
                       values (?, ?, ?, ?)""",
                    (object_id, posting_time, x, y))
        cur.execute("""insert into assets (object_id, asset_name, asset_type)
                       values (?, ?, ?)""",
                    (object_id, asset_name, asset_type))

        # Update whether or not we observed a new position for every
        # plow
        if posting_time > last_posting_time :
            last_posting_time = posting_time
            
        if posting_time > previous_posting_time :
            update_history[object_id].append(1)
            updates += 1
        else :
            update_history[object_id].append(0)

    con.commit()

    # Add the sampling interval
    previous_posting_time = last_posting_time
    if faults :
        intervals.append(sampling_frequency
                         + faults*fault_sleep)
        faults = 0
    else :
        intervals.append(sampling_frequency)

    # Estimate the update rate
    r = [] 
    for object_id in update_history :
        z = sum(update_history[object_id])
        if z > 0 :
            icgm = irregularCGM(intervals, update_history[object_id])
            r.append(fsolve(icgm, .01))

    # Assuming that updates are drawn from a Poisson distribution,
    # then with some probability, we will observe LESS than 2 events
    # in this period. This does not mean the probability that we will
    # observe 1 update, as it is very likely that we will observe no
    # update.
    #
    # We also do not allow the interval to get too small so we don't
    # slam the city's servers.
    #
    # .95 : .355362
    # .90 : .531812
    # .80 : .824388
    #
    # http://www.wolframalpha.com/input/?i=exp(-bx)%2Bbxexp(-bx)%3D.8

    sampling_frequency = max(.824388/max(r), 10)
    print "Estimated Average Update Interval: " + str(int(1/max(r))) + " seconds"
    print "Sampling Interval:                 " + str(int(sampling_frequency)) + " seconds"
    print "Updates:                           " + str(updates)
    print ""
    sleep(sampling_frequency)    

con.close()


#!/usr/bin/python
# -*- coding: utf-8 -*-

import os
import re
import sys
import struct
import json
import requests
import argparse
import pokemon_pb2
import time
import csv
import pytz

from pushbullet import Pushbullet

from google.protobuf.internal import encoder

from datetime import datetime
from datetime import timedelta

from geopy.geocoders import GoogleV3
try:
    from requests.packages.urllib3.exceptions import InsecureRequestWarning
    requests.packages.urllib3.disable_warnings(InsecureRequestWarning)
except ImportError:
    pass
from s2sphere import *

def encode(cellid):
    output = []
    encoder._VarintEncoder()(output.append, cellid)
    return ''.join(output)

def getNeighbors():
    origin = CellId.from_lat_lng(LatLng.from_degrees(FLOAT_LAT, FLOAT_LONG)).parent(15)
    walk = [origin.id()]
    # 10 before and 10 after
    next = origin.next()
    prev = origin.prev()
    for i in range(10):
        walk.append(prev.id())
        walk.append(next.id())
        next = next.next()
        prev = prev.prev()
    return walk

with open('config.json') as file:
	credentials = json.load(file)

PTC_CLIENT_SECRET = credentials.get('PTC_CLIENT_SECRET', None)
ANDROID_ID = credentials.get('ANDROID_ID', None)
SERVICE = credentials.get('SERVICE', None)
CLIENT_SIG = credentials.get('CLIENT_SIG', None)

API_URL = 'https://pgorelease.nianticlabs.com/plfe/rpc'
LOGIN_URL = 'https://sso.pokemon.com/sso/login?service=https%3A%2F%2Fsso.pokemon.com%2Fsso%2Foauth2.0%2FcallbackAuthorize'
LOGIN_OAUTH = 'https://sso.pokemon.com/sso/oauth2.0/accessToken'

SESSION = requests.session()
SESSION.headers.update({'User-Agent': 'Niantic App'})
SESSION.verify = False

DEBUG = False
COORDS_LATITUDE = 0
COORDS_LONGITUDE = 0
COORDS_ALTITUDE = 0
FLOAT_LAT = 0
FLOAT_LONG = 0
deflat, deflng = 0, 0
default_step = 0.001

NUM_STEPS = 10
PKMN_DATA_FILE = 'pkmn.json'
PKSTOP_DATA_FILE = 'pkstop.json'
GYM_DATA_FILE = 'gym.json'
DATA = {
    'pokemon':{},
    'pokestop':{},
    'gym':{}
}

def f2i(float):
  return struct.unpack('<Q', struct.pack('<d', float))[0]

def f2h(float):
  return hex(struct.unpack('<Q', struct.pack('<d', float))[0])

def h2f(hex):
  return struct.unpack('<d', struct.pack('<Q', int(hex,16)))[0]

def prune():
    # prune despawned pokemon
    cur_time = int(time.time())
    for (pokehash, poke) in DATA['pokemon'].items():
        poke['timeleft'] = poke['timeleft'] - (cur_time - poke['timestamp'])
        poke['timestamp'] = cur_time
        if poke['timeleft'] <= 0:
            del DATA['pokemon'][pokehash]

def write_data_to_file():
    prune()

    # different file for bandwith save
    with open(PKMN_DATA_FILE, 'w') as f:
        json.dump(DATA['pokemon'], f, indent=2)

    with open(PKSTOP_DATA_FILE, 'w') as f:
        json.dump(DATA['pokestop'], f, indent=2)

    with open(GYM_DATA_FILE, 'w') as f:
        json.dump(DATA['gym'], f, indent=2)

def add_pokemon(pokeId, name, lat, lng, timestamp, timeleft):
    pokehash = '%s:%s:%s' % (lat, lng, pokeId)
    if pokehash in DATA['pokemon']:
        if abs(DATA['pokemon'][pokehash]['timeleft'] - timeleft) < 2:
            # Assume it's the same one and average the expiry time
            DATA['pokemon'][pokehash]['timeleft'] += timeleft
            DATA['pokemon'][pokehash]['timeleft'] /= 2
        else:
            print('[-] Two %s at the same location (%s,%s)' % (name, lat, lng))
            DATA['pokemon'][pokehash]['timeleft'] = min(DATA['pokemon'][pokehash]['timeleft'], timeleft)
    else:
        DATA['pokemon'][pokehash] = {
            'id': pokeId,
            'name': name,
            'lat': lat,
            'lng': lng,
            'timestamp': timestamp,
            'timeleft': timeleft
        }

def add_pokestop(pokestopId, lat, lng, timeleft):
    if not pokestopId in DATA['pokestop']:
        DATA['pokestop'][pokestopId] = {
            'id': pokestopId,
            'lat': lat,
            'lng': lng,
            'timeleft': timeleft
        }

def add_gym(gymId, team, lat, lng, points):
    if not gymId in DATA['gym']:
        DATA['gym'][gymId] = {
            'id': gymId,
            'team': team,
            'lat': lat,
            'lng': lng,
            'points': points
        }

def set_location(location_name):
    geolocator = GoogleV3()
    prog = re.compile('^(\-?\d+(\.\d+)?),\s*(\-?\d+(\.\d+)?)$')
    global deflat
    global deflng
    if prog.match(location_name):
        local_lat, local_lng = [float(x) for x in location_name.split(",")]
        alt = 0
        deflat, deflng = local_lat, local_lng
    else:
        loc = geolocator.geocode(location_name)
        deflat, deflng = local_lat, local_lng = loc.latitude, loc.longitude
        alt = loc.altitude
        print '[!] Your given location: {}'.format(loc.address.encode('utf-8'))

    print('[!] lat/long/alt: {} {} {}'.format(local_lat, local_lng, alt))
    set_location_coords(local_lat, local_lng, alt)


def set_location_coords(lat, lng, alt):
    global COORDS_LATITUDE, COORDS_LONGITUDE, COORDS_ALTITUDE
    global FLOAT_LAT, FLOAT_LONG
    FLOAT_LAT = lat
    FLOAT_LONG = lng
    COORDS_LATITUDE = f2i(lat) # 0x4042bd7c00000000 # f2i(lat)
    COORDS_LONGITUDE = f2i(lng) # 0xc05e8aae40000000 #f2i(lng)
    COORDS_ALTITUDE = f2i(alt)

def get_location_coords():
    return (COORDS_LATITUDE, COORDS_LONGITUDE, COORDS_ALTITUDE)

def api_req(api_endpoint, access_token, *mehs, **kw):
    while True:
        try:
            p_req = pokemon_pb2.RequestEnvelop()
            p_req.rpc_id = 1469378659230941192

            p_req.unknown1 = 2

            p_req.latitude, p_req.longitude, p_req.altitude = get_location_coords()

            p_req.unknown12 = 989

            if 'useauth' not in kw or not kw['useauth']:
                p_req.auth.provider = 'ptc'
                p_req.auth.token.contents = access_token
                p_req.auth.token.unknown13 = 14
            else:
                p_req.unknown11.unknown71 = kw['useauth'].unknown71
                p_req.unknown11.unknown72 = kw['useauth'].unknown72
                p_req.unknown11.unknown73 = kw['useauth'].unknown73

            for meh in mehs:
                p_req.MergeFrom(meh)

            protobuf = p_req.SerializeToString()

            r = SESSION.post(api_endpoint, data=protobuf, verify=False)

            p_ret = pokemon_pb2.ResponseEnvelop()
            p_ret.ParseFromString(r.content)

            if DEBUG:
                print("REQUEST:")
                print(p_req)
                print("Response:")
                print(p_ret)
                print("\n\n")

            if DEBUG:
                print("[ ] Sleeping for 1 second")
            time.sleep(0.51)
            return p_ret
        except Exception, e:
            if DEBUG:
                print(e)
            print('[-] API request error, retrying')
            time.sleep(0.51)
            continue

def get_profile(access_token, api, useauth, *reqq):
    req = pokemon_pb2.RequestEnvelop()

    req1 = req.requests.add()
    req1.type = 2
    if len(reqq) >= 1:
        req1.MergeFrom(reqq[0])

    req2 = req.requests.add()
    req2.type = 126
    if len(reqq) >= 2:
        req2.MergeFrom(reqq[1])

    req3 = req.requests.add()
    req3.type = 4
    if len(reqq) >= 3:
        req3.MergeFrom(reqq[2])

    req4 = req.requests.add()
    req4.type = 129
    if len(reqq) >= 4:
        req4.MergeFrom(reqq[3])

    req5 = req.requests.add()
    req5.type = 5
    if len(reqq) >= 5:
        req5.MergeFrom(reqq[4])

    return api_req(api, access_token, req, useauth = useauth)

def get_api_endpoint(access_token, api = API_URL):
    p_ret = get_profile(access_token, api, None)
    try:
        return ('https://%s/rpc' % p_ret.api_url)
    except:
        return None


def login_ptc(username, password):
    print('[!] login for: {}'.format(username))
    head = {'User-Agent': 'Niantic App'}
    r = SESSION.get(LOGIN_URL, headers=head)

    try:
        jdata = json.loads(r.content)
    except ValueError, e:
        if DEBUG:
            debug('login_ptc: could not decode JSON from {}'.format(r.content))
        return None

    # Maximum password length is 15 (sign in page enforces this limit, API does not)

    if len(password) > 15:
        print '[!] Trimming password to 15 characters'
        password = password[:15]

    data = {
        'lt': jdata['lt'],
        'execution': jdata['execution'],
        '_eventId': 'submit',
        'username': username,
        'password': password,
    }
    r1 = SESSION.post(LOGIN_URL, data=data, headers=head)

    ticket = None
    try:
        ticket = re.sub('.*ticket=', '', r1.history[0].headers['Location'])
    except Exception, e:
        if DEBUG:
            print(r1.json()['errors'][0])
        return None

    data1 = {
        'client_id': 'mobile-app_pokemon-go',
        'redirect_uri': 'https://www.nianticlabs.com/pokemongo/error',
        'client_secret': PTC_CLIENT_SECRET,
        'grant_type': 'refresh_token',
        'code': ticket,
    }
    r2 = SESSION.post(LOGIN_OAUTH, data=data1)
    access_token = re.sub('&expires.*', '', r2.content)
    access_token = re.sub('.*access_token=', '', access_token)
    return access_token

def raw_heartbeat(api_endpoint, access_token, response):
    m4 = pokemon_pb2.RequestEnvelop.Requests()
    m = pokemon_pb2.RequestEnvelop.MessageSingleInt()
    m.f1 = int(time.time() * 1000)
    m4.message = m.SerializeToString()
    m5 = pokemon_pb2.RequestEnvelop.Requests()
    m = pokemon_pb2.RequestEnvelop.MessageSingleString()
    m.bytes = "05daf51635c82611d1aac95c0b051d3ec088a930"
    m5.message = m.SerializeToString()

    walk = sorted(getNeighbors())

    m1 = pokemon_pb2.RequestEnvelop.Requests()
    m1.type = 106
    m = pokemon_pb2.RequestEnvelop.MessageQuad()
    m.f1 = ''.join(map(encode, walk))
    m.f2 = "\000\000\000\000\000\000\000\000\000\000\000\000\000\000\000\000\000\000\000\000\000"
    m.lat = COORDS_LATITUDE
    m.long = COORDS_LONGITUDE
    m1.message = m.SerializeToString()
    response = get_profile(
        access_token,
        api_endpoint,
        response.unknown7,
        m1,
        pokemon_pb2.RequestEnvelop.Requests(),
        m4,
        pokemon_pb2.RequestEnvelop.Requests(),
        m5)
    try:
        payload = response.payload[0]
    except (AttributeError, IndexError):
        return
    heartbeat = pokemon_pb2.ResponseEnvelop.HeartbeatPayload()
    heartbeat.ParseFromString(payload)
    return heartbeat

def heartbeat(api_endpoint, access_token, response):
    while True:
        try:
            h = raw_heartbeat(api_endpoint, access_token, response)
            return h
        except Exception, e:
            if DEBUG:
                print(e)
            print('[-] Heartbeat missed, retrying')


def scan(api_endpoint, access_token, response, origin, pokemons, pushbullet = None, pokeWantedList = None, csvFile = None):
    steps = 0
    steplimit = NUM_STEPS
    pos = 1
    x   = 0
    y   = 0
    dx  = 0
    dy  = -1
    while steps < steplimit**2:
        original_lat = FLOAT_LAT
        original_long = FLOAT_LONG
        parent = CellId.from_lat_lng(LatLng.from_degrees(FLOAT_LAT, FLOAT_LONG)).parent(15)

        h = heartbeat(api_endpoint, access_token, response)
        hs = [h]
        seen = set([])
        for child in parent.children():
            latlng = LatLng.from_point(Cell(child).get_center())
            set_location_coords(latlng.lat().degrees, latlng.lng().degrees, 0)
            hs.append(heartbeat(api_endpoint, access_token, response))
        set_location_coords(original_lat, original_long, 0)

        visible = []

        for hh in hs:
            try:
                for cell in hh.cells:
                    for wild in cell.WildPokemon:
                        hash = wild.SpawnPointId + ':' + str(wild.pokemon.PokemonId)
                        if (hash not in seen):
                            visible.append(wild)
                            seen.add(hash)
                    if cell.Fort:
                        for Fort in cell.Fort:
                            if Fort.Enabled == True:
                                if Fort.GymPoints:
                                    add_gym(Fort.FortId, Fort.Team, Fort.Latitude, Fort.Longitude, Fort.GymPoints)
                                elif Fort.FortType:
                                    expire_time = 0
                                    if Fort.LureInfo.LureExpiresTimestampMs:
                                        expire_time = datetime\
                                            .fromtimestamp(Fort.LureInfo.LureExpiresTimestampMs / 1000.0)\
                                            .strftime("%H:%M:%S")
                                    add_pokestop(Fort.FortId, Fort.Latitude, Fort.Longitude, expire_time)

            except AttributeError:
                break

        for poke in visible:
            other = LatLng.from_degrees(poke.Latitude, poke.Longitude)
            diff = other - origin
            # print(diff)
            difflat = diff.lat().degrees
            difflng = diff.lng().degrees

            print("(%s) %s is visible at (%s, %s) for %s seconds" % (poke.pokemon.PokemonId, pokemons[poke.pokemon.PokemonId - 1]['Name'], poke.Latitude, poke.Longitude, poke.TimeTillHiddenMs / 1000))

            timestamp = int(time.time())
            add_pokemon(poke.pokemon.PokemonId, pokemons[poke.pokemon.PokemonId - 1]['Name'], poke.Latitude, poke.Longitude, timestamp, poke.TimeTillHiddenMs / 1000)

            
            if pokeWantedList is not None:
                pokeID = poke.pokemon.PokemonId
                if pokeID in pokeWantedList:
                    m, s = divmod(poke.TimeTillHiddenMs / 1000, 60)
                    h, m = divmod(m, 60)
                    curTime = datetime.now()
                    spawnTime = curTime + timedelta(seconds=(poke.timeTillHiddenMs / 1000)) - timedelta(minutes=15)
                    
                    csvFile = open('pokeDump.csv', 'ab')
                    csvWriter = csv.writer(csvFile)
                    csvWriter.writerow([poke.pokemon.PokemonId] + [pokemons[poke.pokemon.PokemonId - 1]['Name']] + [poke.Latitude] + [poke.Longitude] + spawnTime)
                    #csvWriter.writerow([poke.pokemon.PokemonId] + [pokemons[poke.pokemon.PokemonId - 1]['Name']] + [poke.Latitude] + [poke.Longitude] + [poke.TimeTillHiddenMs / 1000] + [datetime.now()])
                    csvFile.close()
                    
                    if pushbullet is not None:
                        pushTitle = "(%s) %s until %s:%s:%s" % (poke.pokemon.PokemonId, pokemons[poke.pokemon.PokemonId - 1]['Name'], curTime.hour + h, curTime.minute + m, curTime.second + s)
                        pushLink = "http://maps.google.com/maps?z=12&t=m&q=loc:%s+%s" % (poke.Latitude, poke.Longitude)            
                        push = pushbullet.push_link(pushTitle, pushLink)

                        

        write_data_to_file()

        if (-steplimit/2 < x <= steplimit/2) and (-steplimit/2 < y <= steplimit/2):
            set_location_coords((x * 0.0025) + deflat, (y * 0.0025 ) + deflng, 0)
        if x == y or (x < 0 and x == -y) or (x > 0 and x == 1-y):
            dx, dy = -dy, dx
        x, y = x+dx, y+dy
        steps +=1

        print('[+] Scan: %0.1f %%' % (((steps + (pos * .25) - .25) / steplimit**2) * 100))


def main():
    full_path = os.path.realpath(__file__)
    (path, filename) = os.path.split(full_path)

    write_data_to_file()
    pokemons = json.load(open(path + '/pokemon.json'))
    parser = argparse.ArgumentParser()
    parser.add_argument("-u", "--username", help="PTC Username", required=True)
    parser.add_argument("-p", "--password", help="PTC Password", required=True)
    parser.add_argument("-l", "--location", help="Location", required=True)
    parser.add_argument("-d", "--debug", help="Debug Mode", action='store_true')
    parser.add_argument("-pb", "--pushbullet", help="Pushbullet API key (optional)", required=False)
    parser.set_defaults(DEBUG=False)
    args = parser.parse_args()

    if args.debug:
        global DEBUG
        DEBUG = True
        print('[!] DEBUG mode on')

    set_location(args.location)

    while True:
        access_token = login_ptc(args.username, args.password)
        if access_token is None:
            print('[-] Error logging in: possible wrong username/password')
            return
        print('[+] RPC Session Token: {} ...'.format(access_token[:25]))

        api_endpoint = get_api_endpoint(access_token)
        if api_endpoint is None:
            print('[-] RPC server offline')
            return
        print('[+] Received API endpoint: {}'.format(api_endpoint))

        response = get_profile(access_token, api_endpoint, None)
        if response is not None:
            print('[+] Login successful')
            
            if args.pushbullet is not None:
                pushbulletID = Pushbullet(args.pushbullet)

            payload = response.payload[0]
            profile = pokemon_pb2.ResponseEnvelop.ProfilePayload()
            profile.ParseFromString(payload)
            print('[+] Username: {}'.format(profile.profile.username))

            creation_time = datetime.fromtimestamp(int(profile.profile.creation_time)/1000)
            print('[+] You are playing Pokemon Go since: {}'.format(
                creation_time.strftime('%Y-%m-%d %H:%M:%S'),
            ))

            for curr in profile.profile.currency:
                print('[+] {}: {}'.format(curr.type, curr.amount))
        else:
            print('[-] Ooops...')

        origin = LatLng.from_degrees(FLOAT_LAT, FLOAT_LONG)
        
        pokeWantedList = [1,2,3,4,5,6,7,8,9,23,24,25,26,27,28,31,34,35,36,37,38,39,40,57,58,59,63,64,65,66,67,68,73,74,75,76,81,82,83,86,88,89,93,94,95,100,101,103,104,105,106,107,108,113,114,115,117,122,123,124,125,126,130,131,132,137,138,139,140,141,142,143,144,145,146,147,148,149,150,151]

        for _x in range(5):
            scan(api_endpoint, access_token, response, origin, pokemons, pushbullet = pushbulletID, pokeWantedList = pokeWantedList)


if __name__ == '__main__':
    main()

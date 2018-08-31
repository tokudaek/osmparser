#!/usr/bin/env python3
"""Parse OSM data
"""

import numpy as np
import numpy
import numpy.linalg
from numpy.linalg import norm as norm
import argparse
import xml.etree.ElementTree as ET
from rtree import index
import matplotlib.pyplot as plt
import logging
from logging import debug, warning
import random
import time

########################################################## DEFS
WAY_TYPES = ["motorway", "trunk", "primary", "secondary", "tertiary",
             "unclassified", "residential", "service", "living_street"]
MAX_ARRAY_SZ = 1048576  # 2^20

##########################################################
def parse_nodes(root, invways):
    """Get all nodes in the xml struct

    Args:
    root(ET): root element 
    invways(dict): inverted list of ways, i.e., node as key and list of way ids as values

    Returns:

    """
    t0 = time.time() 
    valid = invways.keys()
    nodeshash = {}

    for child in root:
        if child.tag != 'node': continue # not node
        if int(child.attrib['id']) not in valid: continue # non relevant node

        att = child.attrib
        lat, lon = float(att['lat']), float(att['lon'])
        nodeshash[int(att['id'])] = (lat, lon)
    debug('Found {} (traversable) nodes ({:.3f}s)'.format(len(nodeshash.keys()),
                                                          time.time() - t0))

    return nodeshash

##########################################################
def parse_ways(root):
    """Get all ways in the xml struct

    Args:
    root(ET): root element 

    Returns:
    dict of list: hash of wayids as key and list of nodes as values;
    dict of list: hash of nodeid as key and list of wayids as values;
    """

    t0 = time.time()
    ways = {}
    invways = {} # inverted list of ways

    for way in root:
        if way.tag != 'way': continue
        wayid = int(way.attrib['id'])
        isstreet = False
        nodes = []

        nodes = []
        for child in way:
            if child.tag == 'nd':
                nodes.append(int(child.attrib['ref']))
            elif child.tag == 'tag':
                if child.attrib['k'] == 'highway' and child.attrib['v'] in WAY_TYPES:
                    isstreet = True

        if isstreet:
            ways[wayid] = nodes

            for node in nodes:
                if node in invways.keys(): invways[node].append(wayid)
                else: invways[node] = [wayid]

    debug('Found {} ways ({:.3f}s)'.format(len(ways.keys()), time.time() - t0))
    return ways, invways

##########################################################
def render_map(nodeshash, ways, crossings, artpoints, queries, counts, frontend='bokeh'):
    if frontend == 'matplotlib':
        render_matplotlib(nodeshash, ways, crossings, artpoints, queries, counts)
    else:
        render_bokeh(nodeshash, ways, crossings, artoints, queries, counts)

##########################################################
def get_nodes_coords_from_hash(nodeshash):
    """Get nodes coordinates and discard nodes ids information
    Args:
    nodeshash(dict): nodeid as key and (x, y) as value

    Returns:
    np.array(n, 2): Return a two-column table containing all the coordinates
    """

    nnodes = len(nodeshash.keys())
    nodes = np.ndarray((nnodes, 2))

    for j, coords in enumerate(nodeshash.values()):
        nodes[j, 0] = coords[0]
        nodes[j, 1] = coords[1]
    return nodes

##########################################################
def get_crossings(invways):
    """Get crossings

    Args:
    invways(dict of list): inverted list of the ways. It s a dict of nodeid as key and
    list of wayids as values

    Returns:
    set: set of crossings
    """

    crossings = set()
    for nodeid, waysids in invways.items():
        if len(waysids) > 1:
            crossings.add(nodeid)
    return crossings

##########################################################
def filter_out_orphan_nodes(ways, invways, nodeshash):
    """Check consistency of nodes in invways and nodeshash and fix them in case
    of inconsistency
    It can just be explained by the *non* exitance of nodes, even though they are
    referenced inside ways (<nd ref>)

    Args:
    invways(dict of list): nodeid as key and a list of wayids as values
    nodeshash(dict of 2-uple): nodeid as key and (x, y) as value
    ways(dict of list): wayid as key and an ordered list of nodeids as values

    Returns:
    dict of list, dict of lists
    """

    ninvways = len(invways.keys())
    nnodeshash = len(nodeshash.keys())
    if ninvways == nnodeshash: return ways, invways

    validnodes = set(nodeshash.keys())

    # Filter ways
    for wayid, nodes in ways.items():
        newlist = [ nodeid for nodeid in nodes if nodeid in validnodes ]
        ways[wayid] = newlist
        
    # Filter invways
    invwaysnodes = set(invways.keys())
    invalid = invwaysnodes.difference(validnodes)

    for nodeid in invalid:
        del invways[nodeid]

    ninvways = len(invways.keys())
    debug('Filtered {} orphan nodes.'.format(ninvways - nnodeshash))
    return ways, invways

##########################################################
def get_segments(ways, crossings):
    """Get segments, given the ways and the crossings

    Args:
    ways(dict of list): hash of wayid as key and list of nodes as values
    crossings(set): set of nodeids in crossings

    Returns:
    dict of list: hash of segmentid as key and list of nodes as value
    dict of list: hash of nodeid as key and list of sids as value
    """

    t0 = time.time() 
    segments = {}
    invsegments = {}

    sid = 0 # segmentid
    segment = []
    for w, nodes in ways.items():
        if not crossings.intersection(set(nodes)):
            segments[sid] = nodes
            sid += 1
            continue

        segment = []
        for node in nodes:
            segment.append(node)

            if node in crossings and len(segment) > 1:
                segments[sid] = segment
                for snode in segment:
                    if snode in invsegments: invsegments[snode].append(sid) 
                    else: invsegments[snode] = [sid]
                segment = [node]
                sid += 1

        segments[sid] = segment # Last segment
        for snode in segment:
            if snode in invsegments: invsegments[snode].append(sid) 
            else: invsegments[snode] = [sid]
        sid += 1

    debug('Found {} segments ({:.3f}s)'.format(len(segments.keys()), time.time() - t0))
    return segments, invsegments

##########################################################
def evenly_space_segment(segment, nodeshash, epsilon):
    """Evenly space one segment

    Args:
    segment(list): nodeids composing the segment
    nodeshash(dict of list): hash with nodeid as value and 2-uple as value

    Returns:
    coords(ndarray(., 2)): array of coordinates
    """
    #debug(segment)
    prevnode = np.array(nodeshash[segment[0]])
    points = [prevnode]

    for nodeid in segment[1:]:
        node = np.array(nodeshash[nodeid])
        d = norm(node - prevnode)
        if d < epsilon:
            points.append(node)
            prevnode = node
            continue

        nnewnodes = int(d / epsilon)
        direc = node - prevnode
        direc = direc / norm(direc)

        cur = prevnode
        for i in range(nnewnodes):
            newnode = cur + direc*epsilon
            points.append(newnode)
            cur = newnode
        prevnode = node
    return np.array(points)

##########################################################
def evenly_space_segments(segments, nodeshash, epsilon=0.0001):
    """Evenly space all segments and create artificial points

    Args:
    segments(dict of list): hash of segmentid as key and list of nodes as values
    nodeshash(dict of list): hash with nodeid as value and 2-uple as value

    Returns:
    points(ndarray(., 3)): rows represents points and first and second columns
    represent coordinates and third represents the segmentid
    """

    t0 = time.time()
    points = np.ndarray((MAX_ARRAY_SZ, 3))
    idx = 0
    for sid, segment in segments.items():
        coords = evenly_space_segment(segment, nodeshash, epsilon)
        n, _ = coords.shape
        points[idx:idx+n, 0:2] = coords
        points[idx:idx+n, 2] = sid
        idx = idx + n
    debug('New {} support points ({:.3f}s)'.format(idx, time.time() - t0))
    return points[:idx, :]

##########################################################
def create_rtree(points, nodeshash, invsegments, invways):
    """short-description

    Args:
    params

    Returns:
    ret

    Raises:
    """

    t0 = time.time()
    pointsidx = index.Index()

    for pid, p in enumerate(points):
        lat, lon  = p[0:2]
        sid = int(p[2])
        pointsidx.insert(pid, (lat, lon, lat, lon), sid)
        #debug('pid:{}, {}, {}'.format(pid, lat, lon))
    
    debug('R-tree created ({:.3f}s)'.format(time.time() - t0))

    return pointsidx

##########################################################
def test_query(pointsidx, points):
    t0 = time.time()
    for i in range(10):
        idx = np.random.randint(1000)
        x0 = points[idx, 0]
        y0 = points[idx, 0]
        querycoords = (x0, y0, x0, y0)
        list(pointsidx.nearest(querycoords, num_results=1, objects='raw'))
    elapsed = time.time() - t0
    debug(elapsed)

def get_count_by_segment(csvinput, segments, pointstree):
    #counts = []
    fh = open(csvinput)
    fh.readline()
    #imageid,n,x,y,t
    nsegments = len(segments.keys())
    counts = np.zeros(nsegments)
    denom = np.zeros(nsegments)
    #denom = np.ones(nsegments)

    from pyproj import Proj, transform
    inProj = Proj(init='epsg:3857')
    outProj = Proj(init='epsg:4326')
    #x1,y1 = -11705274.6374,4826473.6922

    nerrors = 0
    maxcount = 0
    for i, line in enumerate(fh):
        arr = line.split(',')
        count = int(arr[1])
        #print('##########################################################')
        #print(arr)
        if not arr[2]:
            nerrors += 1
            continue
        lon = float(arr[2])
        lat = float(arr[3])
        lon, lat = transform(inProj,outProj,lon,lat)
        if count > maxcount: maxcount = count

        querycoords = (lat, lon, lat, lon) 
        #debug(lat)
        #debug(lon)
        sid = list(pointstree.nearest(querycoords, num_results=1, objects='raw'))[0]

        counts[sid] += count
        denom[sid] += 1

        #if i % 10000 == 0: debug('Iteration:{}'.format(i))
        #if i > 100000: break

    for i in range(nsegments):
        if denom[i] > 0:
            counts[i] /= denom[i]
    debug(np.max(counts))
    debug('Max count:{}'.format(maxcount))
    warning('nerrors:{}'.format(nerrors))
    fh.close()
    return counts

##########################################################
def render_matplotlib(nodeshash, ways, crossings, artpoints, queries, avgcounts=[]):
    # render nodes
    nodes = get_nodes_coords_from_hash(nodeshash)
    #plt.scatter(nodes[:, 1], nodes[:, 0], c='blue', alpha=1, s=20)

    # render artificial nodes
    #plt.scatter(artpoints[:, 1], artpoints[:, 0], c='blue', alpha=1, s=20)


    # render ways
    colors = {}
    i = 0
    for wid, wnodes in ways.items():
        i += 1
        r = lambda: random.randint(0,255)
        if avgcounts == np.array([]):
            waycolor = '#%02X%02X%02X' % (r(),r(),r())
        else:
            #debug(int(50+(avgcounts[wid])*10))
            #waycolor = '#%02X%02X%02X' % (127, 127, int(50+(avgcounts[wid])*10))
            waycolor = 'darkblue'
            alpha = avgcounts[wid] / 15
            if alpha > 1: alpha = 1
        colors[wid] = waycolor
        lats = []; lons = []
        for nodeid in wnodes:
            a, o = nodeshash[nodeid]
            lats.append(a)
            lons.append(o)
        plt.plot(lons, lats, linewidth=2, color=waycolor)

    # render queries
    #for q in queries:
        #plt.scatter(q[1], q[0], linewidth=2, color=colors[q[2]])

    # render crossings
    crossingscoords = np.ndarray((len(crossings), 2))
    for j, crossing in enumerate(crossings):
        crossingscoords[j, :] = np.array(nodeshash[crossing])

    #plt.scatter(crossingscoords[:, 1], crossingscoords[:, 0], c='black')
    #plt.axis('equal')
    plt.show()

##########################################################
def render_bokeh(nodeshash, ways, crossings, artpoints):
    nodes = get_nodes_coords_from_hash(nodeshash)

    from bokeh.plotting import figure, show, output_file
    TOOLS="hover,pan,wheel_zoom,reset"
    p = figure(tools=TOOLS)

    # render nodes
    p.scatter(nodes[:, 1], nodes[:, 0], size=10, fill_alpha=0.8,
                        line_color=None)

    # render ways
    for wnodes in ways.values():
        r = lambda: random.randint(0,255)
        waycolor = '#%02X%02X%02X' % (r(),r(),r())
        lats = []; lons = []
        for nodeid in wnodes:
            a, o = nodeshash[nodeid]
            lats.append(a)
            lons.append(o)
        p.line(lons, lats, line_width=2, line_color=waycolor)

    # render crossings
    crossingscoords = np.ndarray((len(crossings), 2))
    for j, crossing in enumerate(crossings):
        crossingscoords[j, :] = np.array(nodeshash[crossing])
    p.scatter(crossingscoords[:, 1], crossingscoords[:, 0], line_color='black')

    output_file("osm-test.html", title="OSM test")

    show(p)  # open a browser
    return


##########################################################
def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('inputosm', help='Input osm file')
    parser.add_argument('--frontend', choices=['bokeh', 'matplotlib'],
                        help='Front-end vis')
    parser.add_argument('--verbose', help='verbose', action='store_true')

    args = parser.parse_args()

    #if args.verbose:
    if True:
        loglevel = args.verbose if logging.DEBUG else logging.ERROR

    logging.basicConfig(level=loglevel)


    tree = ET.parse(args.inputosm)
    root = tree.getroot() # Tag osm

    ways, invways = parse_ways(root)
    nodeshash = parse_nodes(root, invways)
    ways, invways = filter_out_orphan_nodes(ways, invways, nodeshash)
    crossings = get_crossings(invways)
    segments, invsegments = get_segments(ways, crossings)
    artpoints = evenly_space_segments(segments, nodeshash)
    artpointstree = create_rtree(artpoints, nodeshash, invsegments, invways)

    #for nod, val in nodeshash.items():
        #print(val[0], val[1])
        #break

    queried = []
    #for i in range(1000):
        #nartpoints, _ = artpoints.shape
        #idx = np.random.randint(nartpoints)
        #x = np.random.rand()/1000
        #y = np.random.rand()/1000
        #x0 = artpoints[idx, 0] + x
        #y0 = artpoints[idx, 1] + y
        #querycoords = (x0, y0, x0, y0)
        #segid = list(artpointstree.nearest(querycoords, num_results=1, objects='raw'))[0]
        #queried.append([x0, y0, segid])

    queried = np.array(queried)

    csvinput = '/home/frodo/projects/timeseries-vis/20180723-peds_westvillage_workdays_crs3857_snapped.csv'
    mycount = get_count_by_segment(csvinput, segments, artpointstree)
    render_map(nodeshash, segments, crossings, artpoints, queried, mycount, args.frontend)
    
##########################################################
if __name__ == '__main__':
    main()


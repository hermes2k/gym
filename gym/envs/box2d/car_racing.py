import sys, math
import numpy as np
from pdb import set_trace
from PIL import Image
from copy import copy, deepcopy
import os

import Box2D
from Box2D.b2 import (edgeShape, circleShape, fixtureDef, polygonShape, revoluteJointDef, contactListener)
import cv2

import gym
from gym import spaces
from gym.envs.box2d.car_dynamics import Car
from gym.utils import colorize, seeding, EzPickle

import pyglet
from pyglet import gl

# Easiest continuous control task to learn from pixels, a top-down racing environment.
# Discreet control is reasonable in this environment as well, on/off discretisation is
# fine.
#
# State consists of STATE_W x STATE_H pixels.
#
# Reward is -0.1 every frame and +1000/N for every track tile visited, where N is
# the total number of tiles in track. For example, if you have finished in 732 frames,
# your reward is 1000 - 0.1*732 = 926.8 points.
#
# Game is solved when agent consistently gets 900+ points. Track is random every episode.
#
# Episode finishes when all tiles are visited. Car also can go outside of PLAYFIELD, that
# is far off the track, then it will get -100 and die.
#
# Some indicators shown at the bottom of the window and the state RGB buffer. From
# left to right: true speed, four ABS sensors, steering wheel position, gyroscope.
#
# To play yourself (it's rather fast for humans), type:
#
# python gym/envs/box2d/car_racing.py
#
# Remember it's powerful rear-wheel drive car, don't press accelerator and turn at the
# same time.
#
# Created by Oleg Klimov. Licensed on the same terms as the rest of OpenAI Gym.

STATE_W = 96   # less than Atari 160x192
STATE_H = 96
VIDEO_W = 600
VIDEO_H = 400
WINDOW_H = 700
WINDOW_W = int(WINDOW_H*1.5)

SCALE       = 6.0        # Track scale
TRACK_RAD   = 900/SCALE  # Track is heavily morphed circle with this radius
PLAYFIELD   = 2000/SCALE # Game over boundary
FPS         = 50
ZOOM        = 2.7        # Camera zoom, 0.25 to take screenshots, default 2.7
ZOOM_FOLLOW = True       # Set to False for fixed view (don't use zoom)

TRACK_DETAIL_STEP = 21/SCALE
TRACK_TURN_RATE = 0.31
TRACK_WIDTH = 40/SCALE
BORDER = 8/SCALE
BORDER_MIN_COUNT = 4
NUM_TILES_FOR_AVG = 5 # The number of tiles before and after to takeinto account for angle

ROAD_COLOR = [0.4, 0.4, 0.4]

OBSTACLE_NAME  = 'obstacle'
OBSTACLE_VALUE = -10
TILE_NAME      = 'tile'
BORDER_NAME    = 'border'
GRASS_NAME     = 'grass'

# Debug actions
SHOW_NEXT_N_TILES         = 10       # Show the next N tiles
SHOW_ENDS_OF_TRACKS       = 0       # Shows with red dots the end of track
SHOW_START_OF_TRACKS      = 0       # Shows with green dots the end of track
SHOW_INTERSECTION_POINTS  = 1       # Shows with yellow dots the intersections of main track
SHOW_GROUP_INTERSECTIONS  = 1       # Shows each group of intersections in its own hippie color
SHOW_XT_JUNCTIONS         = 0       # Shows in dark and light green the t and x junctions
SHOW_JOINTS               = 0       # Shows joints in white
SHOW_TURNS                = 0       # Shows the 10 hardest turns
SHOW_AXIS                 = 0       # Draws two lines where the x and y axis are
ZOOM_OUT                  = 0       # Shows maps in general and does not do zoom
if ZOOM_OUT: ZOOM         = 0.25    # Complementary to ZOOM_OUT

class FrictionDetector(contactListener):
    def __init__(self, env):
        contactListener.__init__(self)
        self.env = env
    def BeginContact(self, contact):
        self._contact(contact, True)
    def EndContact(self, contact):
        self._contact(contact, False)
    def _contact(self, contact, begin):
        tile = None
        obj = None
        u1 = contact.fixtureA.body.userData
        u2 = contact.fixtureB.body.userData
        if u1 and "road_friction" in u1.__dict__:
            tile = u1
            obj  = u2
        if u2 and "road_friction" in u2.__dict__:
            tile = u2
            obj  = u1
        if not tile: return

        if tile.typename != OBSTACLE_NAME:
            tile.color[0] = ROAD_COLOR[0]
            tile.color[1] = ROAD_COLOR[1]
            tile.color[2] = ROAD_COLOR[2]
        if not obj or "tiles" not in obj.__dict__: return

        # Substracting value of obstacle
        if tile.typename == OBSTACLE_NAME:
            self.env.reward += OBSTACLE_VALUE

        if begin:
                if tile.typename == TILE_NAME:
                    env.add_current_tile(tile.id, tile.lane)
                obj.tiles.add(tile)
                #print tile.road_friction, "ADD", len(obj.tiles)
                if not tile.road_visited:
                    tile.road_visited = True
                    reward_episode = 1000.0/len(self.env.track)

                    # Cliping reward per expisode
                    reward_episode = np.clip(
                            reward_episode, self.env.min_episode_reward, self.env.max_episode_reward)
                    self.env.reward += reward_episode
                    self.env.tile_visited_count += 1
        else:
            obj.tiles.remove(tile)
            env.remove_current_tile(tile.id, tile.lane)
            #print tile.road_friction, "DEL", len(obj.tiles) -- should delete to zero when on grass (this works)

            # Registering last contact with track
            self.env.last_touch_with_track = self.env.t

class CarRacing(gym.Env, EzPickle):
    '''
    Controls some attributes of the game, such as the number of tracks (num_tracks)
    which is a proxy to control the complexity of map, the number of lanes (num_lanes_changes) and
    the probability of finding an obstacle (prob_osbtacle).
    Only call this method once to set the parameters and do not called outside this env, only use
    make method (i.e. init)

    num_tracks:        (int 1)       Number of tracks, in {1,2}, 1: simple, 2: complex, you can
                                     modify the code to allow more than one track, but good beheviour
                                     is not garanted 

    num_lanes:         (int 1)       Number of lanes in track, > 0 ({1,2})

    num_lanes_changes  (int 0)       Number of changes from 2 to 1 or viceversa, this 
                                     is ultimately transform as a probability over the
                                     total number of points in track

    num_obstacles      (flt 0)       The probability of finding an obstacle a point of 
                                     the track, [0,1]

    max_single_lane    (int 0)       The maximum number of tiles that a single lane road
                                     can have before becoming two lanes again

    allow_reverse      (bool 0)      Allow the car going in reverse, if true key.DOWN goes
                                     backwards and action_space changes

    max_time_out       (flt 2.0)     Max time that car is allowed to be outside of track or stoped 
                                     before reseting the env (sending done=True), if 
                                     max_time_out == 0 then car can be outside without any problem.
                                     max_time_out is in "seconds" but given that in training time
                                     goes faster it is really in 1xFPS (max_time_out = 1/FPS means 
                                     the car is allow only to be outside the track for one frame.
                                     Usually FPS is 59, see the constant in this file, but in 
                                     playing time (runing this file) max_time_out is approximately 
                                     in seconds to allow you have a sense of the magnitude 
                                     This is not necessary time of not earning rewards, only time
                                     outside the track or without moving

    grayscale          (bool 0)      Whether or not use grayscale for the state representation,
                                     the state will be 96x96 of values between 0,255 as rbg state

    show_info_panel    (bool 1)      Whether or not show the info black panel at the bottom of the state

    frames_per_state   (int 1)       The number of concatenated frames for the state, =3 means 
                                     that the state will be the last 3 frames of the env

    discretize_actions (str "hard")  How to discretize the action space, a value in {None,"soft", "hard"}. 
                                     None means space is continuous
                                     "hard" actions are 4 [NOTHING, LEFT, RIGHT, ACCELERATE, BREAK]
                                     "soft" actions are 7 [NOTHING, SOFT_LEFT, HARD_LEFT, SOFT_RIGHT, HARD_RIGHT,
                                     SOFT_STRAIGHT, HARD_STRAIGHT, SOFT_BREAK, HARD_BREAK] but "hard method is not
                                     implemented yet

    min_episode_reward (flt -inf)    To limit the min reward the agent can have in an episode, -np.inf means no limit
                                     it is good to control the gradient
                                     Having max and min values for reward makes learning more stable (i.e. less 
                                     variance) but so far it does not make it learn faster

    max_episode_reward (flt +inf)    To limit the max reward the agent can have in an episode, +np.inf means no limit.
                                     It is good to control the learned speed of the car, to avoid high speeds 1 is 
                                     ok. It is also good to control the gradient.
                                     Having max and min values for reward makes learning more stable (i.e. less 
                                     variance) but so far it does not make it learn faster
    '''
    metadata = {
        'render.modes': ['human', 'rgb_array', 'state_pixels'],
        'video.frames_per_second' : FPS
    }

    def __init__(self, **kwargs):
        EzPickle.__init__(self)
        self.seed()
        self.contactListener_keepref = FrictionDetector(self)
        self.world = Box2D.b2World((0,0), contactListener=self.contactListener_keepref)
        self.viewer = None
        self.invisible_state_window = None
        self.invisible_video_window = None
        self.road = None
        self.car = None
        self.reward = 0.0
        self.prev_reward = 0.0
        self.highest_reward = 0.0
        self._current_nodes = {} # A dict of dicts, dict[id][lane]=direction you can be in more than one tile at the same time, e.g. intersections
        self._next_nodes = [] # A list of lists of dictionaries
        self.possible_hard_actions = ("NOTHING", "LEFT", "RIGHT", "ACCELERATE", "BREAK")
        self.possible_soft_actions = ("NOTHING", "SOFT_LEFT", "HARD_LEFT", "SOFT_RIGHT", "HARD_RIGHT",
                "SOFT_ACCELERATE", "HARD_ACCELERATE", "SOFT_BREAK", "HARD_BREAK")

        # Config
        self._set_config(**kwargs)

    def _set_config(self, 
            num_tracks=1, 
            num_lanes=1, 
            num_lanes_changes=0, 
            num_obstacles=0, 
            max_single_lane=0,
            max_time_out=2.0,
            grayscale=False,
            show_info_panel=False,
            frames_per_state=1,
            discretize_actions='hard',
            allow_reverse=0,
            min_episode_reward=-np.inf,
            max_episode_reward=+np.inf,
            ):

        
        # Number of lanes, 1 or 2
        self.num_lanes = num_lanes  if num_lanes in [1,2] else 1

        # Number of tracks, this control the complexity of the map
        self.num_tracks = num_tracks if num_tracks > 0 and num_tracks <= 2 else 1

        # Number of obstacles in the track
        self.num_obstacles = num_obstacles if num_obstacles >= 0 else 0

        # Number of points where lanes change from 1 lane to two and viceversa
        self.num_lanes_changes = num_lanes_changes if num_lanes_changes >= 0 else 0

        # Max number of tiles of a single lane road
        self.max_single_lane = max_single_lane if max_single_lane > 10 else 50

        # Allow reverse
        self.allow_reverse = allow_reverse
        min_speed = -1 if self.allow_reverse else 0

        # Max time out of track
        self.max_time_out = max_time_out if max_time_out >= 0 else 2.0

        # Grayscale
        self.grayscale = grayscale
        state_shape = [STATE_H, STATE_W]
        if not self.grayscale: 
            state_shape.append(3)

        # Show or not back bottom info panel
        self.show_info_panel = show_info_panel

        # Frames per state
        self.frames_per_state = frames_per_state if frames_per_state > 0 else 1
        if self.frames_per_state > 1:
            state_shape.insert(0,self.frames_per_state)

            lst = list(range(self.frames_per_state))
            self._update_index = [lst[-1]] + lst[:-1]

        self.discretize_actions = discretize_actions if discretize_actions in [None,"soft","hard"] else "hard"
        
        if max_episode_reward < min_episode_reward:
            raise AttributeError("max_episode_reward must be greater than min_episode_reward")
        self.max_episode_reward = max_episode_reward
        self.min_episode_reward = min_episode_reward

        state_shape = tuple(state_shape)
        # Incorporating reverse now the np.array([-1,0,0]) becomes np.array[-1,-1,0]
        if self.discretize_actions == "soft":
            self.action_space = spaces.Discrete(len(self.possible_soft_actions))
        elif self.discretize_actions == "hard":
            self.action_space = spaces.Discrete(len(self.possible_hard_actions))
        else:
            self.action_space = spaces.Box( np.array([-1,min_speed,0]), np.array([+1,+1,+1]), dtype=np.float32)  # steer, gas, brake

        self.observation_space = spaces.Box(low=0, high=255, shape=state_shape, dtype=np.uint8)


    def set_velocity(self, velocity=[0.0,0.0]):
        self.car.hull.linearVelocity.Set(velocity[0],velocity[1])

    def set_speed(self, speed):
        ang = self.car.hull.angle + math.pi/2
        velocity_x = math.cos(ang)*speed
        velocity_y = math.sin(ang)*speed
        self.set_velocity([velocity_x, velocity_y])


    def seed(self, seed=None):
        self.np_random, seed = seeding.np_random(seed)
        return [seed]

    def _destroy(self):
        if not self.road: return
        for t in self.road:
            self.world.DestroyBody(t)
        self.road = []
        self.car.destroy()

    def place_agent(self, position):
        '''
        position = [beta,x,y]
        '''
        self.car.destroy()
        self.car = Car(self.world, *position, allow_reverse=self.allow_reverse)

    def add_current_tile(self,id,lane):
        ######## Calculating direction
        id_relative = id
        if self.info[id]['track'] > 0:
            id_relative -= len(self.tracks[self.info[id]['track']-1])
        next_id = (id_relative + 1) % len(self.tracks[self.info[id]['track']])
        last_id = (id_relative - 1) % len(self.tracks[self.info[id]['track']])
        
        keys = self._current_nodes.keys()
        #if id-id_relative+next_id in keys:
        if abs((self.track[id,0,1] - self.car.hull.angle + np.pi/2)%(np.pi*2)) > np.pi :
            direction = -1
        else:
            direction = 1

        ######## Adding it to the current node
        if id in self._current_nodes.keys():
            self._current_nodes[id][lane] = direction
        else:
            self._current_nodes[id] = {lane:direction}
        #######

        ###### Removing current new tile from nexts
        if len(self._next_nodes) > 0:
            if id in self._next_nodes[0] and lane in self._next_nodes[0][id] \
                    and self._next_nodes[0][id][lane] == direction:
                # If the current new tile is a prediction, then there is 
                # no need to predcit all N next tiles again
                if len(self._next_nodes[0][id]) > 1:
                    del self._next_nodes[0][id][lane]
                else:
                    del self._next_nodes[0][id]
            else:
                # If tile is not the next prediction means that the
                # car is somewhere else and we need to predict all 10 again
                self._next_nodes = []

        # Cleaning next_nodes from empty lists
        self._next_nodes = [item for item in self._next_nodes if len(item) > 0]
        #######

        ####### predictions
        while len(self._next_nodes) < SHOW_NEXT_N_TILES:
            if len(self._next_nodes) == 0:
                # if there is no prediction
                elems = self._current_nodes
            else:
                # else take the last predictions
                elems = self._next_nodes[-1]
            next_nodes = {}
            for id,vals in elems.items():
                for lane,direction in vals.items():
                    tmp_preds = [] # This is only used in the case of direction 0,
                    # direction zero means you can take the two directions, that only
                    # happens at an intersection
                    if direction == 0:
                        tmp_preds.append(self._get_next_node(id,lane,+1))
                        tmp_preds.append(self._get_next_node(id,lane,-1))
                    else:
                        tmp_preds.append(self._get_next_node(id,lane,direction))
                    for tmp_pred in tmp_preds:
                        for k,v in tmp_pred.items():
                            if k not in next_nodes: next_nodes[k] = {}
                            for tmp_lane,tmp_dir in v.items():
                                next_nodes[k][tmp_lane] = tmp_dir

            self._next_nodes.append(next_nodes)
        #######

    def remove_current_tile(self,id,lane):
        if id in self._current_nodes:
            if len(self._current_nodes[id]) > 1:
                del self._current_nodes[id][lane]
            else:
                del self._current_nodes[id]
            if len(self._current_nodes) == 0:
                self._next_nodes = []

    def _update_predictions(self):
        self._trail_nodes = {k:v for l in self._next_nodes for k,v in l.items()}
        self._trail_nodes.update(self._current_nodes)

    def _get_next_node(self,id,lane,direction): 
        """
        this will return a dict of elements, elem is a dict of [id][lane] = direction
        """
        # if it is the end of the row or the beginning in the opposite direction
        if (self.info[id]['end'] == True and direction == 1) or \
           (self.info[id]['start'] == True and direction == -1):
            return {}
        else:
            # Else calculate the next tile
            id_relative = id
            if self.info[id]['track'] > 0:
                id_relative -= len(self.tracks[self.info[id]['track']-1])

            next_id = (id_relative + direction) % len(self.tracks[self.info[id]['track']]) # TODO direction 0

            # If the next tile is in an intersection add all the following intersections
            intersection = self.info[id - id_relative + next_id]['intersection_id']
            next_nodes = {}
            if intersection != -1:
                for tmp_id in np.where(self.info['intersection_id'] == intersection)[0]:
                    next_nodes[tmp_id] = {}
                    if self.info[tmp_id]['track'] > 0:
                        if self.info[tmp_id]['end']: 
                            direction = -1
                        if self.info[tmp_id]['start']:
                            direction = -1
                        next_nodes[tmp_id][1] = direction 
                        next_nodes[tmp_id][0] = direction
                    else:
                        # if it is not end or start but still and intersection then it is in the main track
                        if tmp_id == next_id:
                            # if it is the current one keep the direction and lane
                            next_nodes[tmp_id][lane] = direction 
                        else:
                            # add both directions and lanes
                            next_nodes[tmp_id][1] = 0
                            next_nodes[tmp_id][0] = 0
            else:
                if not id-id_relative+next_id in next_nodes.keys():
                    next_nodes[id-id_relative+next_id] = {}
                next_nodes[id - id_relative + next_id][lane] = direction
            return next_nodes

    def _get_track(self, CHECKPOINTS, TRACK_RAD=900/SCALE):

        CHECKPOINTS = 12

        # Create checkpoints
        checkpoints = []
        for c in range(CHECKPOINTS):
            alpha = 2*math.pi*c/CHECKPOINTS + self.np_random.uniform(0, 2*math.pi*1/CHECKPOINTS)
            rad = self.np_random.uniform(TRACK_RAD/3, TRACK_RAD)
            if c==0:
                alpha = 0
                rad = 1.5*TRACK_RAD
            if c==CHECKPOINTS-1:
                alpha = 2*math.pi*c/CHECKPOINTS
                self.start_alpha = 2*math.pi*(-0.5)/CHECKPOINTS
                rad = 1.5*TRACK_RAD
            checkpoints.append( (alpha, rad*math.cos(alpha), rad*math.sin(alpha)) )

        #print "\n".join(str(h) for h in checkpoints)
        #self.road_poly = [ (    # uncomment this to see checkpoints
        #    [ (tx,ty) for a,tx,ty in checkpoints ],
        #    (0.7,0.7,0.9) ) ]
        self.road = []

        # Go from one checkpoint to another to create track
        x, y, beta = 1.5*TRACK_RAD, 0, 0
        dest_i = 0
        laps = 0
        track = []
        no_freeze = 2500
        visited_other_side = False
        while 1:
            alpha = math.atan2(y, x)
            if visited_other_side and alpha > 0:
                laps += 1
                visited_other_side = False
            if alpha < 0:
                visited_other_side = True
                alpha += 2*math.pi
            while True: # Find destination from checkpoints
                failed = True
                while True:
                    dest_alpha, dest_x, dest_y = checkpoints[dest_i % len(checkpoints)]
                    if alpha <= dest_alpha:
                        failed = False
                        break
                    dest_i += 1
                    if dest_i % len(checkpoints) == 0: break
                if not failed: break
                alpha -= 2*math.pi
                continue
            r1x = math.cos(beta)
            r1y = math.sin(beta)
            p1x = -r1y
            p1y = r1x
            dest_dx = dest_x - x  # vector towards destination
            dest_dy = dest_y - y
            proj = r1x*dest_dx + r1y*dest_dy  # destination vector projected on rad
            while beta - alpha >  1.5*math.pi: beta -= 2*math.pi
            while beta - alpha < -1.5*math.pi: beta += 2*math.pi
            prev_beta = beta
            proj *= SCALE
            if proj >  0.3: beta -= min(TRACK_TURN_RATE, abs(0.001*proj))
            if proj < -0.3: beta += min(TRACK_TURN_RATE, abs(0.001*proj))
            x += p1x*TRACK_DETAIL_STEP
            y += p1y*TRACK_DETAIL_STEP
            track.append( (alpha,prev_beta*0.5 + beta*0.5,x,y) )
            if laps > 4: break
            no_freeze -= 1
            if no_freeze==0: break
        #print "\n".join([str(t) for t in enumerate(track)])

        # Find closed loop range i1..i2, first loop should be ignored, second is OK
        i1, i2 = -1, -1
        i = len(track)
        while True:
            i -= 1
            if i==0: return False  # Failed
            pass_through_start = track[i][0] > self.start_alpha and track[i-1][0] <= self.start_alpha
            if pass_through_start and i2==-1:
                i2 = i
            elif pass_through_start and i1==-1:
                i1 = i
                break
        print("Track generation: %i..%i -> %i-tiles track" % (i1, i2, i2-i1))
        assert i1!=-1
        assert i2!=-1

        track = track[i1:i2-1]

        first_beta = track[0][1]
        first_perp_x = math.cos(first_beta)
        first_perp_y = math.sin(first_beta)
        # Length of perpendicular jump to put together head and tail
        well_glued_together = np.sqrt(
            np.square( first_perp_x*(track[0][2] - track[-1][2]) ) +
            np.square( first_perp_y*(track[0][3] - track[-1][3]) ))
        if well_glued_together > TRACK_DETAIL_STEP:
            return False

        track = [[track[i-1],track[i]] for i in range(len(track))]
        return track

    def _create_obstacles(self):
        # Get random tile, with replacement
        # Create obstacle (red rectangle of random width and position in tile)
        tiles_idx = np.random.choice(range(len(self.track)), self.num_obstacles, replace=False)
        for idx in tiles_idx:
            alpha, beta, x,y = self._get_rnd_position_inside_lane(idx)

            width = abs(np.random.normal(1)*TRACK_WIDTH/4)

            p1 = (x - width*math.cos(beta) + TRACK_DETAIL_STEP/2*math.sin(beta),
                  y - width*math.sin(beta) - TRACK_DETAIL_STEP/2*math.cos(beta))
            p2 = (x + width*math.cos(beta) + TRACK_DETAIL_STEP/2*math.sin(beta),
                  y + width*math.sin(beta) - TRACK_DETAIL_STEP/2*math.cos(beta))
            p3 = (x + width*math.cos(beta) - TRACK_DETAIL_STEP/2*math.sin(beta),
                  y + width*math.sin(beta) + TRACK_DETAIL_STEP/2*math.cos(beta))
            p4 = (x - width*math.cos(beta) - TRACK_DETAIL_STEP/2*math.sin(beta),
                  y - width*math.sin(beta) + TRACK_DETAIL_STEP/2*math.cos(beta))

            vertices = [p1,p2,p3,p4]

            # Add it to obstacles
            # Add it to poly_obstacles
            t = self.world.CreateStaticBody( fixtures = fixtureDef(
                shape=polygonShape(vertices=vertices)
                ))
            t.userData = t
            t.color = [0.86,0.08,0.23] 
            t.road_friction = 1.0
            t.road_visited  = True
            t.typename = OBSTACLE_NAME
            t.fixtures[0].sensor = True
            self.obstacles_poly.append(( vertices, t.color ))
            self.road.append(t)

    def _create_info(self):
        '''
        Creates the matrix with the information about the track points,
        whether they are at the end of the track, if they are intersections
        '''
        # Get if point is at the end
        info  = np.zeros((sum(len(t) for t in self.tracks)),dtype=[
            ('track', 'int'),
            ('end','bool'),
            ('begining', 'bool'),
            ('intersection', 'bool'),
            ('intersection_id', 'int'),
            ('t','bool'),
            ('x','bool'),
            ('start','bool'),
            ('used','bool'),
            ('angle', 'float16'),
            ('ang_class','float16'),
            ('lanes',np.ndarray),
            ('obstacles',np.ndarray)])

        info['ang_class'] = np.nan
        info['intersection_id'] = -1

        for i in range(len(info)):
            info[i]['lanes'] = [True, True]

        for i in range(1, len(self.tracks)): 
            track = self.tracks[i]
            info[len(self.tracks[i-1]):len(self.tracks[i-1])+len(track)]['track'] = i # This wont work for num_tracks > 2
            for j in range(len(track)):
                pos = j + len(self.tracks[i-1])
                p = track[j]
                next_p = track[(j+1)%len(track)]
                last_p = track[j-1]
                if np.array_equal(p[1], next_p[0]) == False:
                    # it is at the end
                    info[pos]['end'] = True
                elif np.array_equal(p[0], last_p[1]) == False:
                    # it is at the start
                    info[pos]['start'] = True

        # Trying to get all intersections
        intersections = set()
        if self.num_tracks > 1:
            for pos, point1 in enumerate(self.tracks[0][:,1,2:]):
                d = np.linalg.norm(self.track[len(self.tracks[0]):,1,2:]-point1,axis=1)
                if d.min() <= 2.05*TRACK_WIDTH:
                    intersections.add(pos)

            intersections = list(intersections)
            intersections.sort()
            track_len = len(self.tracks[0])
        
            def backwards():
                me = intersections[-1]
                del intersections[-1]
                if len(intersections) == 0: return [me]
                else:
                    if (me-1)%track_len == intersections[-1]:
                        return [me]+backwards()
                    else:
                        return [me]

            def forward():
                me = intersections[0]
                del intersections[0]
                if len(intersections) == 0: return [me]
                else:
                    if (me+1)%track_len == intersections[0]:
                        return [me]+forward()
                    else:
                        return [me]

            groups = []
            tmp_lst = []
            while len(intersections) != 0:
                me = intersections[0]
                tmp_lst = tmp_lst + backwards()
                if len(intersections) != 0:
                    if (me-1)%track_len == intersections[-1]:
                        tmp_lst = tmp_lst + forward()

                groups.append(tmp_lst)
                tmp_lst = []

            for group in groups:
                min_dist_idx = None
                min_dist     = 1e10
                for idx in group:
                    d = np.linalg.norm(self.track[track_len:,1,2:] - self.track[idx,1,2:],axis=1)
                    if d.min() < min_dist:
                        min_dist     = d.min()
                        min_dist_idx = idx
                
                if min_dist <= TRACK_WIDTH:
                    intersections.append(min_dist_idx)

            info['intersection'][list(intersections)] = True

            # Classifying intersections
            for idx in intersections:
                point = self.track[idx,1,2:]
                d = np.linalg.norm(self.track[:,1,2:]-point, axis=1)
                argmin = d[info['track'] != 0].argmin()
                filt = np.where(d < TRACK_WIDTH*2.5)

                # TODO ignore intersections with angles of pi/2

                if info[filt]['start'].sum() - info[filt]['end'].sum() != 0:
                    info[idx]['t'] = True
                    info[argmin + track_len]['t'] = True                
                else: 
                    # the sum can be zero because second tracks are not cutted in case of x
                    info[idx]['x'] = True
                    info[argmin + track_len]['x'] = True                

        # Getting angles of curves
        max_idxs = []
        self.track[:,0,1] = np.mod(self.track[:,0,1], 2*math.pi)
        self.track[:,1,1] = np.mod(self.track[:,1,1], 2*math.pi)
        for num_track in range(self.num_tracks):

            track = self.tracks[num_track]
            angles = track[:,0,1] - track[:,1,1]
            inters = np.logical_or(info[info['track'] == num_track]['t'],info[info['track'] == num_track]['x'])

            track_len_compl = (info['track'] < num_track).sum()
            track_len       = len(track)

            while np.abs(angles).max() != 0.0:
                max_rel_idx = np.abs(angles).argmax()

                rel_idxs    = [(max_rel_idx + j)%track_len for j in range(-NUM_TILES_FOR_AVG  ,NUM_TILES_FOR_AVG  ) ]
                idxs_safety = [(max_rel_idx + j)%track_len for j in range(-NUM_TILES_FOR_AVG*2,NUM_TILES_FOR_AVG*2) ]
                
                if (inters[idxs_safety] == True).sum() == 0:
                    max_idxs.append(max_rel_idx + track_len_compl)
                    angles[rel_idxs] = 0.0
                else:
                    angles[max_rel_idx] = 0.0

        info['angle'][max_idxs] = self.track[max_idxs,0,1] - self.track[max_idxs,1,1]
        

        ######### populating intersection_id
        intersection_dict = {}

        # Remove keys which are to close
        intersection_keys = np.where(info['intersection'])[0]
        intersection_vals = np.where((info['x']) | (info['t']))[0]
            
        for val in intersection_vals:
            tmp = self.track[intersection_keys][:,1,2:]
            elm = self.track[val,1,2:]
            d = np.linalg.norm(tmp-elm,axis=1)
            if d.min() > TRACK_WIDTH*2:
                print("the closest intersection is too far away")
            else:
                k = intersection_keys[d.argmin()] 
                
                if k in intersection_dict.keys(): pass
                else: 
                    intersection_dict[k] = []

                intersection_dict[k].append(val)
        
        self.intersection_dict = intersection_dict

        for k,v in self.intersection_dict.items():
            info['intersection_id'][[k]+v] = k
        del self.intersection_dict
        ##############################################
        
        self.info = info

    def _set_lanes(self):
        if self.num_lanes_changes > 0 and self.num_lanes > 1:
            rm_lane = 0 # 1 remove lane, 0 keep lane
            lane    = 0 # Which lane will be removed
            changes = np.sort(self.np_random.randint(0,len(self.track),self.num_lanes_changes))

            # check in changes work
            # There must be no change at least 50 pos before and end and after a start
            changes_bad = []
            for pos, idx in enumerate(changes):
                start_from = sum(self.info['track'] <  self.info[idx]['track'])
                until      = sum(self.info['track'] == self.info[idx]['track'])
                changes_in_track = np.subtract(changes, start_from)
                changes_in_track = changes_in_track[(changes_in_track < until)*(changes_in_track > 0)]
                idx_relative = idx - start_from 

                if sum(((changes_in_track - idx) > 0)*((changes_in_track - idx) < 10)) > 0: # TODO wont work when at end of track
                    changes_bad.append(idx)
                    next

                track_info   = self.info[self.info['track'] == self.info[idx]['track']]
                for i in range(50+1):
                    if track_info[(idx_relative+i)%len(track_info)]['end'] or track_info[idx_relative-i]['start']:
                        changes_bad.append(idx)
                        break

            if len(changes_bad) > 0:
                changes = np.setdiff1d(changes,changes_bad)

            counter = 0 # in order to avoid more than max number of single lanes tiles
            for i, point in enumerate(self.track):
                change = True if i in changes else False
                rm_lane = (rm_lane+change)%2

                if change and rm_lane == 1: # if it is time to change and the turn is to remove lane
                    lane = np.random.randint(0,2,1)[0]

                if rm_lane:
                    self.info[i]['lanes'][lane] = False
                    counter +=1
                else:
                    counter  =0

                # Change if end/inter of or if change prob
                if self.info[i]['end'] or self.info[i]['start'] or counter > self.max_single_lane: 
                    rm_lane = 0

            # Avoiding any change of lanes in last and beginning part of a track
            for num_track in range(self.num_tracks):
                for lane in range(self.num_lanes):
                    for i in range(10):
                        self.info[self.info['track'] == num_track][+i]['lanes'][lane] = True
                        self.info[self.info['track'] == num_track][-i]['lanes'][lane] = True

        
    def _create_track(self):

        tracks = []
        for _ in range(self.num_tracks):
            track = self._get_track(12)
            if not track or len(track) == 0: return track
            track = np.array(track)
            tracks.append(track)

        self.tracks = tracks
        if self._remove_roads() == False: return False

        self.track = np.concatenate(self.tracks)

        self._create_info()
        self._set_lanes()
    
        # Red-white border on hard turns
        borders = []
        for track in self.tracks:
            border = [False]*len(track)
            for i in range(1,len(track)):
                good = True
                oneside = 0
                for neg in range(BORDER_MIN_COUNT):
                    beta1 = track[i-neg][1][1]
                    beta2 = track[i-neg][0][1]
                    good &= abs(beta1 - beta2) > TRACK_TURN_RATE*0.2
                    oneside += np.sign(beta1 - beta2)
                good &= abs(oneside) == BORDER_MIN_COUNT
                border[i] = good
            for i in range(len(track)):
                for neg in range(BORDER_MIN_COUNT):
                    border[i-neg] |= border[i]
            borders.append(border)
                
        # Creating borders for printing
        pos = 0
        for j in range(self.num_tracks):
            track  = self.tracks[j]
            border = borders[j]
            for i in range(len(track)):
                alpha1, beta1, x1, y1 = track[i][1]
                alpha2, beta2, x2, y2 = track[i][0]
                if border[i]:
                    side = np.sign(beta2 - beta1)

                    c = 1

                    # Addapting border to appear at the right widht when there are different number of lanes
                    if self.num_lanes > 1:
                        if side == -1 and self.info[pos]['lanes'][0] == False: c = 0
                        if side == +1 and self.info[pos]['lanes'][1] == False: c = 0

                    b1_l = (x1 + side* TRACK_WIDTH*c        *math.cos(beta1), y1 + side* TRACK_WIDTH*c        *math.sin(beta1))
                    b1_r = (x1 + side*(TRACK_WIDTH*c+BORDER)*math.cos(beta1), y1 + side*(TRACK_WIDTH*c+BORDER)*math.sin(beta1))
                    b2_l = (x2 + side* TRACK_WIDTH*c        *math.cos(beta2), y2 + side* TRACK_WIDTH*c        *math.sin(beta2))
                    b2_r = (x2 + side*(TRACK_WIDTH*c+BORDER)*math.cos(beta2), y2 + side*(TRACK_WIDTH*c+BORDER)*math.sin(beta2))
                    self.border_poly.append(( [b1_l, b1_r, b2_r, b2_l], (1,1,1) if i%2==0 else (1,0,0) ))
                pos += 1


        # Create tiles
        for j in range(len(self.track)):
            obstacle = np.random.binomial(1,0)
            alpha1, beta1, x1, y1 = self.track[j][1]
            alpha2, beta2, x2, y2 = self.track[j][0]

            for lane in range(self.num_lanes):
                if self.info[j]['lanes'][lane]:
                    
                    joint = False # to differentiate joints from normal tiles

                    r = 1- ((lane+1)%self.num_lanes)
                    l = 1- ((lane+2)%self.num_lanes)

                    # Get if it is the first or last
                    first = False # first of lane
                    last  = False # last tile of line

                    if self.info[j]['end'] == False and self.info[j]['start'] == False:

                        # Getting if first tile of lane
                        # if last tile was from the same lane
                        info_track = self.info[self.info['track'] == self.info[j]['track']]
                        j_relative = j - sum(self.info['track'] < self.info[j]['track'])
                        
                        if info_track[j_relative-1]['track'] == info_track[j_relative]['track']:
                            # If last tile didnt exist
                            if info_track[j_relative-1]['lanes'][lane] == False:
                                first = True
                        if info_track[(j_relative+1)%len(info_track)]['track'] == info_track[j_relative]['track']:
                            # If last tile didnt exist
                            if info_track[(j_relative+1)%len(info_track)]['lanes'][lane] == False:
                                last = True

                    road1_l = (x1 - (1-last) *l*TRACK_WIDTH*math.cos(beta1), y1 - (1-last) *l*TRACK_WIDTH*math.sin(beta1))
                    road1_r = (x1 + (1-last) *r*TRACK_WIDTH*math.cos(beta1), y1 + (1-last) *r*TRACK_WIDTH*math.sin(beta1))
                    road2_l = (x2 - (1-first)*l*TRACK_WIDTH*math.cos(beta2), y2 - (1-first)*l*TRACK_WIDTH*math.sin(beta2))
                    road2_r = (x2 + (1-first)*r*TRACK_WIDTH*math.cos(beta2), y2 + (1-first)*r*TRACK_WIDTH*math.sin(beta2))

                    vertices = [road1_l, road1_r, road2_r, road2_l]

                    if self.info[j]['end'] == True or self.info[j]['start'] == True:

                        points = [] # to store the new points
                        p3 = [] # in order to save all points 3 to create joints
                        for i in [0,1]: # because there are two point to do
                            # Get the closest point to a line make by the continuing trend of the original road points, the points will be the points 
                            # under a radius r from line to avoid taking points far away in the other extreme of the track
                            # Remember the distance from a point p3 to a line p1,p2 is d = norm(np.cross(p2-p1, p1-p3))/norm(p2-p1)
                            # p1=(x1,y1)+sin/cos, p2=(x2,y2)+sin/cos, p3=points in poly
                            if self.info[j]['end']:
                                p1 = road1_l if i == 0 else road1_r
                                p2 = road2_l if i == 0 else road2_r
                            else:
                                p1 = road1_l if i == 0 else road1_r
                                p2 = road2_l if i == 0 else road2_r

                            if len(p3) == 0:
                                max_idx = sum(sum(self.info[self.info['track'] == 0]['lanes'],[])) # this will work because only seconday tracks have ends
                                p3_org = sum([x[0] for x in self.road_poly[:max_idx]],[])
                                # filter p3 by distance to p1 < TRACK_WIDTH*2
                                distance = TRACK_WIDTH*2
                                not_too_close = np.where(np.linalg.norm(np.subtract(p3_org,p1),axis=1) >= TRACK_WIDTH/3)[0]
                                while len(p3) == 0 and distance < PLAYFIELD:
                                    close = np.where(np.linalg.norm(np.subtract(p3_org,p1),axis=1) <= distance)[0]
                                    p3 = [p3_org[i] for i in np.intersect1d(close,not_too_close)]
                                    distance += TRACK_WIDTH

                            if len(p3) == 0:
                                raise RuntimeError('p3 lenght is zero')

                            d = (np.cross(np.subtract(p2,p1),np.subtract(p1,p3)))**2/np.linalg.norm(np.subtract(p2,p1))
                            points.append(p3[d.argmin()])

                        if self.info[j]['start']:
                            vertices = [points[0], points[1], road1_r, road1_l]
                        else:
                            vertices = [road2_r, road2_l, points[0], points[1]]
                        joint = True

                    t = self.world.CreateStaticBody( fixtures = fixtureDef(
                        shape=polygonShape(vertices=vertices)
                        ))
                    t.userData = t
                    c = 0.01*(i%3)
                    if joint and SHOW_JOINTS:
                        t.color = [1,1,1]
                    else:
                        t.color = [ROAD_COLOR[0], ROAD_COLOR[1], ROAD_COLOR[2]] 
                    t.road_visited = False
                    t.typename = TILE_NAME
                    t.road_friction = 1.0
                    t.id = j
                    t.lane = lane
                    t.fixtures[0].sensor = True
                    self.road_poly.append(( vertices, t.color, t.id, t.lane ))
                    self.road.append(t)

        self._create_obstacles()

        return True

    def reset(self):
        '''
        car_position [angle float, x float, y float]
                     Position of the car
                     Default: first tile of principal track
        '''
        self._destroy()
        self.reward = 0.0
        self.highest_reward = 0.0
        self.last_touch_with_track = 0.0
        self.prev_reward = 0.0
        self.tile_visited_count = 0
        self.t = 0.0
        self._current_nodes = {}
        self._next_nodes = []
        self.road_poly = []
        self.border_poly = []
        self.obstacles_poly = []
        self.track = []
        self.tracks = []
        self.human_render = False
        self.state = np.zeros(self.observation_space.shape)

        while True:
            success = self._create_track()
            if success: break
            print("retry to generate track (normal if there are not many of this messages)")
        #if car_position is None or car_position[0] == None or car_position[1] == None or car_position[2] == None:
        car_position = self.track[0][1][1:4]
        self.car = Car(self.world, *car_position, allow_reverse=self.action_space)
        self.place_agent(self.get_rnd_point_in_track())

        return self.step(None)[0]

    def _update_state(self,new_frame):
        if self.frames_per_state > 1:
            self.state[-1] = new_frame
            self.state = self.state[self._update_index]
        else:
            self.state = new_frame

    def _transform_action(self, action):
        if self.discretize_actions == "soft":
            raise NotImplementedError
        elif self.discretize_actions == "hard":
            # ("NOTHING", "LEFT", "RIGHT", "ACCELERATE", "BREAK")
            # angle, gas, break
            if action == 0: action = [ 0, 0, 0.0] # Nothing
            if action == 1: action = [-1, 0, 0.0] # Left
            if action == 2: action = [+1, 0, 0.0] # Right
            if action == 3: action = [ 0,+1, 0.0] # Accelerate
            if action == 4: action = [ 0, 0, 0.8] # break

        return action

    def step(self, action):
        action = self._transform_action(action)

        if action is not None:
            self.car.steer(-action[0])
            self.car.gas(action[1])
            self.car.brake(action[2])

        self.car.step(1.0/FPS)
        self.world.Step(1.0/FPS, 6*30, 2*30)
        self.t += 1.0/FPS

        # self.state = self.render("state_pixels") # Old code, only one frame
        self._update_state(self.render("state_pixels"))

        step_reward = 0
        done = False
        if action is not None: # First step without action, called from reset()
            self.reward -= 0.1
            # We actually don't want to count fuel spent, we want car to be faster.
            #self.reward -=  10 * self.car.fuel_spent / ENGINE_POWER
            self.car.fuel_spent = 0.0
            step_reward = self.reward - self.prev_reward
            self.prev_reward = self.reward
            if self.tile_visited_count==len(self.track) or \
                    (self.t - self.last_touch_with_track > self.max_time_out and \
                    self.max_time_out > 0.0):
                done = True
                if self.t - self.last_touch_with_track > self.max_time_out and \
                        self.max_time_out > 0.0:
                    print("done by time")
                    step_reward = -100
            x, y = self.car.hull.position
            if not done and abs(x) > PLAYFIELD or abs(y) > PLAYFIELD:
                done = True
                step_reward = -100
                

        return self.state, step_reward, done, {}

    def render(self, mode='human'):
        if self.viewer is None:
            from gym.envs.classic_control import rendering
            self.viewer = rendering.Viewer(WINDOW_W, WINDOW_H)
            self.score_label = pyglet.text.Label('0000', font_size=36,
                x=20, y=WINDOW_H*2.5/40.00, anchor_x='left', anchor_y='center',
                color=(255,255,255,255))
            self.transform = rendering.Transform()

        if "t" not in self.__dict__: return  # reset() not called yet

        zoom = 0.1*SCALE*max(1-self.t, 0) + ZOOM*SCALE*min(self.t, 1)   # Animate zoom first second
        zoom_state  = ZOOM*SCALE*STATE_W/WINDOW_W
        zoom_video  = ZOOM*SCALE*VIDEO_W/WINDOW_W
        scroll_x = self.car.hull.position[0]
        scroll_y = self.car.hull.position[1]
        angle = -self.car.hull.angle
        vel = self.car.hull.linearVelocity
        # The angle is the same as the car, not as the speed
        #if np.linalg.norm(vel) > 0.5:
        #    angle = math.atan2(vel[0], vel[1])
        self.transform.set_scale(zoom, zoom)
        if ZOOM_OUT:
            self.transform.set_translation(WINDOW_W/2, WINDOW_H/2)
            self.transform.set_rotation(0) 
        else:
            self.transform.set_translation(
                WINDOW_W/2 - (scroll_x*zoom*math.cos(angle) - scroll_y*zoom*math.sin(angle)), 
                WINDOW_H/4 - (scroll_x*zoom*math.sin(angle) + scroll_y*zoom*math.cos(angle)) )
            self.transform.set_rotation(angle)

        self.car.draw(self.viewer, mode!="state_pixels")

        arr = None
        win = self.viewer.window
        if mode != 'state_pixels':
            win.switch_to()
            win.dispatch_events()
        if mode=="rgb_array" or mode=="state_pixels":
            win.clear()
            t = self.transform
            if mode=='rgb_array':
                VP_W = VIDEO_W
                VP_H = VIDEO_H
            else:
                VP_W = STATE_W
                VP_H = STATE_H
            gl.glViewport(0, 0, VP_W, VP_H)
            t.enable()
            self.render_road()
            self.render_road_lines()
            for geom in self.viewer.onetime_geoms:
                geom.render()
            t.disable()
            if self.show_info_panel:
                self.render_indicators(WINDOW_W, WINDOW_H)  # TODO: find why 2x needed, wtf
            image_data = pyglet.image.get_buffer_manager().get_color_buffer().get_image_data()
            arr = np.fromstring(image_data.data, dtype=np.uint8, sep='')
            arr = arr.reshape(VP_H, VP_W, 4)
            arr = arr[::-1, :, 0:3]
            if self.grayscale:
                arr_bw = np.dot(arr[...,:3], [0.299, 0.587, 0.114])
                arr = arr_bw
            
        if mode=="rgb_array" and not self.human_render: # agent can call or not call env.render() itself when recording video.
            win.flip()

        if mode=='human':
            self.human_render = True
            win.clear()
            t = self.transform
            gl.glViewport(0, 0, WINDOW_W, WINDOW_H)
            t.enable()
            self.render_road()
            self.render_road_lines()
            for geom in self.viewer.onetime_geoms:
                geom.render()
            t.disable()
            self.render_indicators(WINDOW_W, WINDOW_H)
            win.flip()

        self.viewer.onetime_geoms = []
        return arr
    
    def screenshot(self, dest="./", name=None):
        ''' 
        Saves the current state
        '''
        state = self.state
        if state is not None:
            for f in range(self.frames_per_state):

                if self.frames_per_state == 1:
                    frame_str = ""
                    frame = state
                else:
                    frame_str = "_frame%i" % f
                    frame = state[f]

                if self.grayscale:
                    frame = np.stack([frame,frame,frame], axis=-1)

                frame = frame.astype(np.uint8)
                im = Image.fromarray(frame)
                if name == None: name = "screenshot_%0.3f" % self.t
                im.save("%s/%s%s.jpeg" % (dest, name, frame_str))

    def close(self):
        if self.viewer is not None:
            self.viewer.close()
            self.viewer = None

    def _remove_roads(self):

        if self.num_tracks > 1:
            def _get_section(first,last,track):
                sec = []
                pos = 0
                found = False
                while 1:
                    point = track[pos%track.shape[0],:,2:]
                    if np.linalg.norm(point[1]-first) <= TRACK_WIDTH/2:
                        found = True
                    if found:
                        sec.append(point)
                        if np.linalg.norm(point[1]-last) <= TRACK_WIDTH/2:
                            break
                    pos = pos+1
                    if pos / track.shape[0] >= 2: break
                if sec == []: return False
                return np.array(sec)

            THRESHOLD = TRACK_WIDTH*2

            track1 = np.array(self.tracks[0])
            track2 = np.array(self.tracks[1])

            points1 = track1[:,:,[2,3]]
            points2 = track2[:,:,[2,3]]

            inter2 = np.array([x for x in points2 if (np.linalg.norm(points1[:,1,:]-x[1:], axis=1) <= TRACK_WIDTH/3.5 ).sum() >= 1])

            intersections = []
            for i in range(inter2.shape[0]):
                if np.array_equal(inter2[i-1,1,:],inter2[i,0,:]) == False or np.array_equal(inter2[i,1,:], inter2[((i+1)%len(inter2)),0,:]) == False:
                    intersections.append(inter2[i])
            intersections = np.array(intersections)

            # For each point in intersection
            # > get section of both roads
            # > For each point in section in second road
            # > > get min distance
            # > get max of distances
            # if max dist < threshold remove
            removed_idx = set()
            intersection_keys = []
            intersection_vals = []
            for i in range(intersections.shape[0]):
                _, first = intersections[i-1]
                last,_ = intersections[i]

                sec1 = _get_section(first,last,track1)
                sec2 = _get_section(first,last,track2)
                
                if sec1 is not False and sec2 is not False:
                    max_min_d = 0
                    remove = False
                    for point in sec1[:,1]:
                        dist = np.linalg.norm(sec2[:,1] - point, axis=1).min()
                        max_min_d = dist if max_min_d < dist else max_min_d
                    # TODO here the roads that are very very close to the other 
                    # track or that for several tiles keeps very close to the 
                    # road can be removed
                    if max_min_d < THRESHOLD*2: remove = True
                    
                    # Removing tiles
                    if remove:
                        for point in sec2:
                            idx = np.all(track2[:,:,[2,3]] == point, axis=(1,2))
                            removed_idx.update(np.where(idx)[0])
                    else:
                        # TODO save where the connections belong  
                        key = np.where(
                                np.all(track1[:,:,[2,3]] == sec1[0], axis=(1,2)))[0]
                        val = np.where(
                                np.all(track2[:,:,[2,3]] == sec2[0], axis=(1,2)))[0]\
                                        + len(track1)
                        intersection_keys.append(key[0])
                        intersection_vals.append(val[0])

                        key = np.where(
                                np.all(track1[:,:,[2,3]] == sec1[-1], axis=(1,2)))[0]
                        val = np.where(
                                np.all(track2[:,:,[2,3]] == sec2[-1], axis=(1,2)))[0]\
                                        + len(track1)
                        intersection_keys.append(key[0])
                        intersection_vals.append(val[0])

            track2 = np.delete(track2, list(removed_idx), axis=0) # efficient way to delete them from np.array

            self.intersections = intersections
            
            if len(track1) == 0 or len(track2) == 0:
                return False

            self.tracks[0] = track1
            self.tracks[1] = track2

            return True

    def _render_tiles(self):
        '''
        Can only be called inside a glBegin
        '''
        # drawing road old way
        for poly, color, id, lane in self.road_poly:
            if id in self._trail_nodes and lane in self._trail_nodes[id]:
                color = [c/2 for c in color]

            gl.glColor4f(color[0], color[1], color[2], 1)
            for p in poly:
                gl.glVertex3f(p[0], p[1], 0)

    def _render_obstacles(self):
        '''
        Can only be called inside a glBegin
        '''
        # drawing road old way
        for poly, color in self.obstacles_poly:
            gl.glColor4f(color[0], color[1], color[2], 1)
            for p in poly:
                gl.glVertex3f(p[0], p[1], 0)

    def render_road(self):
        gl.glBegin(gl.GL_QUADS)
        gl.glColor4f(0.4, 0.8, 0.4, 1.0)
        gl.glVertex3f(-PLAYFIELD, +PLAYFIELD, 0)
        gl.glVertex3f(+PLAYFIELD, +PLAYFIELD, 0)
        gl.glVertex3f(+PLAYFIELD, -PLAYFIELD, 0)
        gl.glVertex3f(-PLAYFIELD, -PLAYFIELD, 0)
        gl.glColor4f(0.4, 0.9, 0.4, 1.0)
        k = PLAYFIELD/20.0
        for x in range(-20, 20, 2):
            for y in range(-20, 20, 2):
                gl.glVertex3f(k*x + k, k*y + 0, 0)
                gl.glVertex3f(k*x + 0, k*y + 0, 0)
                gl.glVertex3f(k*x + 0, k*y + k, 0)
                gl.glVertex3f(k*x + k, k*y + k, 0)

        
        self._update_predictions()
        self._render_tiles()
        self._render_obstacles()

        # drawing angles of old config, the 
        # black line is the angle (NOT WORKING)
        if False:
            for track in self.tracks:
                for point1, point2 in track:
                    alpha1,beta1,x1,y1 = point1
                    beta1 = alpha1

                    gl.glColor4f(0, 0, 0, 0)
                    gl.glVertex3f(x1+2,y1, 0)
                    gl.glVertex3f(x1+2+math.cos(beta1)*2,y1+math.sin(beta1)*2, 0)
                    gl.glVertex3f(x1-2+math.cos(beta1)*2,y1+math.sin(beta1)*2, 0)
                    gl.glVertex3f(x1-2,y1, 0)

        # Ploting axis
        if SHOW_AXIS:
            # x-axis
            gl.glColor4f(0, 0, 0, 1)
            gl.glVertex3f(-PLAYFIELD, 2, 0)
            gl.glVertex3f(+PLAYFIELD, 2, 0)
            gl.glVertex3f(+PLAYFIELD,-2, 0)
            gl.glVertex3f(-PLAYFIELD,-2, 0)
            
            # y-axis
            gl.glVertex3f(+2,-PLAYFIELD, 0)
            gl.glVertex3f(+2,+PLAYFIELD, 0)
            gl.glVertex3f(-2,+PLAYFIELD, 0)
            gl.glVertex3f(-2,-PLAYFIELD, 0)

        self.render_debug_clues()
        
        gl.glEnd()

    def render_road_lines(self):
        pass        

    def render_debug_clues(self):

        if SHOW_ENDS_OF_TRACKS:
            for x,y in self.track[self.info['end']][:,1,2:]:
                gl.glColor4f(1, 0, 0, 1)
                gl.glVertex3f(x+2,y+2,0)
                gl.glVertex3f(x-2,y+2,0)
                gl.glVertex3f(x-2,y-2,0)
                gl.glVertex3f(x+2,y-2,0)

        if SHOW_START_OF_TRACKS:
            for x,y in self.track[self.info['start']][:,1,2:]:
                gl.glColor4f(0, 1, 0, 1)
                gl.glVertex3f(x+2,y+2,0)
                gl.glVertex3f(x-2,y+2,0)
                gl.glVertex3f(x-2,y-2,0)
                gl.glVertex3f(x+2,y-2,0)

        if SHOW_INTERSECTION_POINTS:
            for x,y in self.track[self.info['intersection']][:,1,2:]:
                gl.glColor4f(1, 1, 0, 1)
                gl.glVertex3f(x+1,y+1,0)
                gl.glVertex3f(x-1,y+1,0)
                gl.glVertex3f(x-1,y-1,0)
                gl.glVertex3f(x+1,y-1,0)

        if SHOW_GROUP_INTERSECTIONS:
            ids = set(self.info['intersection_id'])
            ids.remove(-1)

            for id in ids:
                np.random.seed(id)
                r = np.random.uniform(size=3)
                for elem in self.track[self.info['intersection_id'] == id]:
                    x,y = elem[1,2:]
                    gl.glColor4f(r[0], r[1], r[2], 1)
                    gl.glVertex3f(x+1,y+1,0)
                    gl.glVertex3f(x-1,y+1,0)
                    gl.glVertex3f(x-1,y-1,0)
                    gl.glVertex3f(x+1,y-1,0)
            
        if SHOW_XT_JUNCTIONS:
            for x,y in self.track[self.info['t']][:,1,2:]:
                gl.glColor4f(0, 0.4, 0, 1)
                gl.glVertex3f(x+1,y+1,0)
                gl.glVertex3f(x-1,y+1,0)
                gl.glVertex3f(x-1,y-1,0)
                gl.glVertex3f(x+1,y-1,0)
            for x,y in self.track[self.info['x']][:,1,2:]:
                gl.glColor4f(6, 0.8, 0.18, 1)
                gl.glVertex3f(x+1,y+1,0)
                gl.glVertex3f(x-1,y+1,0)
                gl.glVertex3f(x-1,y-1,0)
                gl.glVertex3f(x+1,y-1,0)

        if SHOW_TURNS:
            for x,y in self.track[np.abs(self.info['angle']).argsort()[-10:]][:,1,2:]:
                gl.glColor4f(1, 0, 0, 1)
                gl.glVertex3f(x+1,y+1,0)
                gl.glVertex3f(x-1,y+1,0)
                gl.glVertex3f(x-1,y-1,0)
                gl.glVertex3f(x+1,y-1,0)

    def render_indicators(self, W, H):
        gl.glBegin(gl.GL_QUADS)
        s = W/40.0
        h = H/40.0
        gl.glColor4f(0,0,0,1)
        gl.glVertex3f(W, 0, 0)
        gl.glVertex3f(W, 5*h, 0)
        gl.glVertex3f(0, 5*h, 0)
        gl.glVertex3f(0, 0, 0)
        def vertical_ind(place, val, color):
            gl.glColor4f(color[0], color[1], color[2], 1)
            gl.glVertex3f((place+0)*s, h + h*val, 0)
            gl.glVertex3f((place+1)*s, h + h*val, 0)
            gl.glVertex3f((place+1)*s, h, 0)
            gl.glVertex3f((place+0)*s, h, 0)
        def horiz_ind(place, val, color):
            gl.glColor4f(color[0], color[1], color[2], 1)
            gl.glVertex3f((place+0)*s, 4*h , 0)
            gl.glVertex3f((place+val)*s, 4*h, 0)
            gl.glVertex3f((place+val)*s, 2*h, 0)
            gl.glVertex3f((place+0)*s, 2*h, 0)
        true_speed = np.sqrt(np.square(self.car.hull.linearVelocity[0]) + np.square(self.car.hull.linearVelocity[1]))
        vertical_ind(5, 0.02*true_speed, (1,1,1))
        vertical_ind(7, 0.01*self.car.wheels[0].omega, (0.0,0,1)) # ABS sensors
        vertical_ind(8, 0.01*self.car.wheels[1].omega, (0.0,0,1))
        vertical_ind(9, 0.01*self.car.wheels[2].omega, (0.2,0,1))
        vertical_ind(10,0.01*self.car.wheels[3].omega, (0.2,0,1))
        horiz_ind(20, -10.0*self.car.wheels[0].joint.angle, (0,1,0))
        horiz_ind(30, -0.8*self.car.hull.angularVelocity, (1,0,0))
        gl.glEnd()
        self.score_label.text = "%04i" % self.reward
        self.score_label.draw()

    def get_rnd_point_in_track(self,border=True):
        '''
        returns a random point in the track with the angle equal 
        to the tile of the track, the x position can be randomly 
        in the x (relative) axis of the tile, border=True make 
        sure the x position is enough to make the car fit in 
        the track, otherwise the point can be in the extreme 
        of the track and two wheels will be outside the track
        -----
        Returns: [beta, x, y]
        '''
        idx = self.np_random.randint(0, len(self.track))
        _,beta,x,y = self._get_rnd_position_inside_lane(idx,border)
        return [beta, x, y]

    def _get_rnd_position_inside_lane(self,idx,border=True):
        '''
        idx of tile
        '''
        alpha, beta, x, y = self.track[idx,1,:]
        r,l = True, True
        if self.num_lanes > 1:
            l,r = self.info[idx]['lanes']
        from_val = -TRACK_WIDTH*l + border*TRACK_WIDTH/2
        to_val   = +TRACK_WIDTH*r - border*TRACK_WIDTH/2
        h = np.random.uniform(from_val,to_val) 
        x += h*math.cos(alpha)
        y += h*math.sin(alpha)
        return [alpha,beta,x,y]

    def get_position_near_junction(self,type_junction, tiles_before):
        '''
        type_junction (str) : 't' or 'x' so far
        tiles_before  (int) : number of tiles before the t junction, can be
                              negative as well
        '''
        if (self.info[type_junction] == True).sum() > 0:
            idx = np.random.choice(np.where(self.info[type_junction] == True)[0])
            idx_relative = idx - (self.info['track'] < self.info[idx]['track']).sum()
            track = self.track[self.info['track'] == self.info[idx]['track']]
            idx_general = (idx_relative + tiles_before)%len(track) + (self.info['track'] < self.info[idx]['track']).sum()
            _, beta, x, y = self._get_rnd_position_inside_lane(idx_general)
            if tiles_before > 0: beta += math.pi
            return [beta,x,y]
        else:
            return False

    def get_position_outside(self, distance):
        '''
        Returns a random position outside the track with random angle 

        bear in mind that distance can be negative
        '''
        idx   = np.random.randint(0,len(self.track))
        angle = np.random.uniform(0,2*math.pi)
        _,beta,x,y = self.track[idx,1,:]
        r,l = True, True
        if self.num_lanes > 1:
            l,r = self.info[idx]['lanes']
        if distance > 0:
            x = x + (r*TRACK_WIDTH + distance)*math.cos(beta)
            y = y + (r*TRACK_WIDTH + distance)*math.sin(beta)
        else:
            x = x - (l*TRACK_WIDTH + abs(distance))*math.cos(beta)
            y = y - (l*TRACK_WIDTH + abs(distance))*math.sin(beta)

        return [angle,x,y]

    def switch_intersection_groups(self):
        global SHOW_GROUP_INTERSECTIONS
        SHOW_GROUP_INTERSECTIONS += 1
        SHOW_GROUP_INTERSECTIONS %= 2
        
    def switch_end_of_track(self):
        global SHOW_ENDS_OF_TRACKS
        SHOW_ENDS_OF_TRACKS += 1
        SHOW_ENDS_OF_TRACKS %= 2

    def switch_start_of_track(self):
        global SHOW_START_OF_TRACKS
        SHOW_START_OF_TRACKS += 1
        SHOW_START_OF_TRACKS %= 2

    def change_zoom(self):
        global ZOOM_OUT, ZOOM
        ZOOM_OUT = (ZOOM_OUT+1)%2
        if ZOOM_OUT: ZOOM = 0.25
        else:        ZOOM = 2.7

if __name__=="__main__":
    from pyglet.window import key

    # whether or not discretize env
    discretize = None # or "hard", "soft", None

    if discretize == None:
        a = np.array( [0.0, 0.0, 0.0] )
    else:
        a = np.array([0])
    def key_press(k, mod):
        global restart
        if discretize == None:
            if k==0xff0d: restart = True
            if k==key.LEFT:  a[0] = -1.0
            if k==key.RIGHT: a[0] = +1.0
            if k==key.UP:    a[1] = +1.0
            if k==key.DOWN:  a[1] = -1.0
            if k==key.SPACE: a[2] = +0.8   # set 1.0 for wheels to block to zero rotation
        elif discretize == "hard":
            if k==0xff0d: restart = True
            if k==key.LEFT:  a[0] = 1
            if k==key.RIGHT: a[0] = 2
            if k==key.UP:    a[0] = 3
            if k==key.SPACE: a[0] = 4
    def key_release(k, mod):
        if discretize == None:
            if k==key.LEFT  and a[0]==-1.0: a[0] = 0
            if k==key.RIGHT and a[0]==+1.0: a[0] = 0
            if k==key.UP:    a[1] = 0
            if k==key.DOWN:  a[1] = 0
            if k==key.SPACE: a[2] = 0
        else:
            a[0] = 0
        if k==key.D:     set_trace()
        if k==key.R:     env.reset()
        if k==key.Z:     env.change_zoom()
        if k==key.G:     env.switch_intersection_groups()
        if k==key.E:     env.switch_end_of_track()
        if k==key.S:     env.switch_start_of_track()
        if k==key.Q:     sys.exit()

    env = CarRacing(
            allow_reverse=False, 
            grayscale=0,
            show_info_panel=1,
            discretize_actions=discretize,
            num_tracks=2,
            num_lanes=2,
            num_lanes_changes=4,
            max_time_out=0,
            frames_per_state=4)

    env.render()
    record_video = False
    if record_video:
        env.monitor.start('/tmp/video-test', force=True)
    env.viewer.window.on_key_press = key_press
    env.viewer.window.on_key_release = key_release
    while True:
        env.reset()
        total_reward = 0.0
        steps = 0
        restart = False

        while True:
            if discretize != None: a_tmp = a[0]
            else: a_tmp = a
            s, r, done, info = env.step(a_tmp)
            total_reward += r
            if steps % 200 == 0 or done:
                #print("\naction " + str(["{:+0.2f}".format(x) for x in a]))
                print("step {} total_reward {:+0.2f}".format(steps, total_reward))
                #import matplotlib.pyplot as plt
                #plt.imshow(s)
                #plt.savefig("test.jpeg")
            steps += 1
            if not record_video: # Faster, but you can as well call env.render() every time to play full window.
                env.render()
            if done or restart: break

            # every 100 steps save screenshot
            if False:
                if not os.path.exists('./screenshots'):
                    os.makedirs('./screenshots')
                if steps % 200 == 0:
                    env.screenshot("./screenshots")
                    pass

    env.close()

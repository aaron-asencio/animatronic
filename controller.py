#from trunkcontroller import TrunkController
from movements import Movements
from animatronic import Animatronic
import asyncio
import concurrent.futures
import argparse
import subprocess
from subprocess import Popen

# Arguments
parser = argparse.ArgumentParser()

parser.add_argument('--action', default=None, help='Action to perform')

args = parser.parse_args()

mv = Movements()
a = Animatronic()
       
#async def main():
print(args.action)

proc = None

if (args.action == 'mic'):
    cmd = 'sudo /usr/bin/python /home/pi/lightshowpi/py/synchronized_lights.py --config="/home/pi/lightshowpi/config/overrides-mic.cfg"'
    proc = subprocess.Popen(cmd, shell=True)
    # this spawns two processes
    # sometimes jaw sticks
    # need to be able to terminate this process - have to send ctrl + C to it 
    # might to run process in separate thread 
    #proc.terminate()
elif (args.action == 'wave'):
    asyncio.run(mv.wave())
elif(args.action == 'yes'):
    asyncio.run(mv.nodYes())
elif(args.action == 'no'):
    asyncio.run(mv.shakeNo())
elif(args.action == 'lookAround'):
      asyncio.run(mv.lookAround())
elif(args.action == 'scan'):
      asyncio.run(mv.scan())
elif(args.action == 'swivelHead'):
    asyncio.run(mv.swivelHead())
elif(args.action == 'come'):
    asyncio.run(mv.come())   
elif(args.action == 'comein'):
    asyncio.run(mv.comein())     
elif(args.action == 'armOut'):
    asyncio.run(mv.armOut())        
elif(args.action == 'neckEllipse'):
    asyncio.run(mv.neckEllipse())    
elif(args.action == 'lookAroundSmall'):
    asyncio.run(mv.lookAroundSmall())    
elif(args.action == 'party'):
    a.startParty()
elif(args.action == 'yoda900'):
    a.yoda900()
elif(args.action == 'rippedPants'):
   a.rippedPants()
elif(args.action == 'blah'):
    a.blah()
elif(args.action == 'vaderBeaten'):
    a.vaderBeaten()
elif(args.action == 'exorcist'):
    a.exorcist()
elif(args.action == 'waiting'):
    a.waiting()
elif(args.action == 'vaderFather'):
    a.vaderFather()
elif(args.action == 'bloody'):
    a.bloody()          
elif(args.action == 'yodaFear'):
    a.yodaFear()
elif(args.action == 'audioest'):
    a.audioTest()
else:
    raise Exception('Unknown action '+ str(args.action))  




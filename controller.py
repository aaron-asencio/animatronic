from trunkcontroller import TrunkController
from movements import Movements
import asyncio
import concurrent.futures
import argparse

# Arguments
parser = argparse.ArgumentParser()

parser.add_argument('--action', default=None, help='Action to perform')

trunkController = TrunkController("Servo TrunkController")
 
args = parser.parse_args()

mv = Movements("Orchstrate Movements")
       

print(args.action)

if (args.action == 'wave'):
    asyncio.run(trunkController.wave())
elif(args.action == 'yes'):
   asyncio.run(trunkController.nodYes())
elif(args.action == 'no'):
   asyncio.run(trunkController.shakeNo())
elif(args.action == 'lookAround'):
    mv.lookAround()
elif(args.action == 'scan'):
    mv.scan()
elif(args.action == 'swivelHead'):
    mv.lookAround()


#from trunkcontroller import TrunkController
from movements import Movements
import asyncio
import concurrent.futures
import argparse

# Arguments
parser = argparse.ArgumentParser()

parser.add_argument('--action', default=None, help='Action to perform')

#trunkController = TrunkController("Servo TrunkController")
 
args = parser.parse_args()

mv = Movements("Orchstrate Movements")
       
#async def main():
print(args.action)

if (args.action == 'wave'):
    asyncio.run(mv.wave())
elif(args.action == 'yes'):
    asyncio.run(mv.nodYes())
elif(args.action == 'no'):
    asyncio.run(mv.shakeNo())
elif(args.action == 'lookAround'):
    mv.lookAround()
elif(args.action == 'scan'):
    mv.scan()
elif(args.action == 'swivelHead'):
    asyncio.run(mv.swivelHead())
elif(args.action == 'come'):
    asyncio.run(mv.come())   

#asyncio.run(main())


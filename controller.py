#from trunkcontroller import TrunkController
from movements import Movements
from animatronic import Animatronic
import asyncio
import concurrent.futures
import argparse

# Arguments
parser = argparse.ArgumentParser()

parser.add_argument('--action', default=None, help='Action to perform')

#trunkController = TrunkController("Servo TrunkController")
 
args = parser.parse_args()

mv = Movements()
a = Animatronic()
       
#async def main():
print(args.action)

if (args.action == 'wave'):
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
elif(args.action == 'startParty'):
    asyncio.run(a.startParty())
elif(args.action == 'yoda900'):
    asyncio.run(a.yoda900())
elif(args.action == 'rippedPants'):
    asyncio.run(a.rippedPants())
elif(args.action == 'blah'):
    asyncio.run(a.blah())
elif(args.action == 'vaderBeaten'):
    asyncio.run(a.vaderBeaten())   
elif(args.action == 'exorcist'):
    asyncio.run(a.exorcist())   
elif(args.action == 'waiting'):
    asyncio.run(a.waiting())   
elif(args.action == 'vaderFather'):
    asyncio.run(a.vaderFather())   
elif(args.action == 'bloody'):
    asyncio.run(a.bloody())            
elif(args.action == 'yodaFear'):
    asyncio.run(a.yodaFear())          
else:
    raise Exception('Unknown action '+ str(args.action))  




from trunkcontroller import TrunkContoller
import asyncio
import concurrent.futures

async def main():
    loop = asyncio.get_event_loop()
    contoller = TrunkContoller("Servo TrunkContoller")
    #works but not async
    #await asyncio.gather(contoller.neckPan(), contoller.neckTilt())
    # await asyncio.gather( TrunkContoller("Servo TrunkContoller").neckTilt(), TrunkContoller("Servo TrunkContoller").neckPan() )
    #works but not async
    result1 = await loop.run_in_executor(None, contoller.neckTilt)
    result = await loop.run_in_executor(None, contoller.neckPan)
    
    #task = loop.create_task(foo())
    #loop.run_until_complete(task)
    
    # the ServoKit library is probably not built for asyncio but the run_in_executor should run concurrent. You have to remove
    # async from the in front of the method for run_in_executor.
    # create class without servoKit and test to see if we can't make concurrent calls KISS - try loop with print first to prove.
    
   
    

asyncio.run(main())
    #import sys
    #print(sys.argv[1])


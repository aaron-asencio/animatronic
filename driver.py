from trunkcontroller import TrunkController
import asyncio
import concurrent.futures
import subprocess

def lookAround():
    loop = asyncio.get_event_loop()
    controller = TrunkController("Servo TrunkController")
    neckTilt = loop.create_task(controller.neckTilt())
    neckPan = loop.create_task(controller.neckPan())
    shoulder = loop.create_task(controller.shoulder())
    loop.run_until_complete(asyncio.gather(neckTilt, neckPan, shoulder))
    #asyncio.run_coroutine_threadsafe(controller.neckCenter(), loop) 
    #center = loop.create_task(controller.neckCenter())
    asyncio.run(controller.neckCenter())

def wave():
    #pool = concurrent.futures.ThreadPoolExecutor() 
    #loop = asyncio.get_event_loop()
    #loop = asyncio.get_running_loop()
    controller = TrunkController("Servo TrunkController")
  
    # works
    asyncio.run(controller.wave())
    
    #    asyncio.run(controller.elbowTilt())
    # asyncio.run(controller.elbowPan())
        
    #await loop.run_in_executor(None, controller.wave)
    #_event_loop()
    #wave = loop.create_task(controller.wave())
    #loop.run_until_complete(asyncio.gather(wave))


def main():
    #lookAround()
    
    subprocess.call("/home/pi/lightshowpi/py/synchronized_lights.py --file=$MUSIC_HOME/sb_party_switch.mp3", shell=True)
    #wave()
    #asyncio.run(wave())
    #asyncio.run(controller.wave())
    #works but not async
    #await asyncio.gather(contoller.neckPan(), contoller.neckTilt())
    # await asyncio.gather( TrunkContoller("Servo TrunkContoller").neckTilt(), TrunkContoller("Servo TrunkContoller").neckPan() )
    #works but not async
    #await loop.run_in_executor(None, contoller.neckTilt)
    
main()   
#asyncio.run(main())
    #import sys
    #print(sys.argv[1])


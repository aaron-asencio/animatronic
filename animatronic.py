from synchronized_lights import Lightshow
from animatronic.movements import Movements
import asyncio
import concurrent.futures
import threading
import subprocess
from subprocess import Popen
"""
to call this code to run a file
sudo /usr/bin/python /home/pi/lightshowpi/py/animatronic.py --file="/home/pi/lightshowpi/music/sb_party_switch.mp3"

To put in input mode - this actually started playing xmas playlist - have to look into that
sudo /usr/bin/python /home/pi/lightshowpi/py/animatronic.py --config="/home/pi/lightshowpi/config/overrides-mic.cfg"

"""


def myAsync():
    mv = Movements("Orchestrate Movements")
    #subprocess.call('sudo /usr/bin/python /home/pi/lightshowpi/py/synchronized_lights.py --file="/home/pi/lightshowpi/music/sb_party_switch.mp3"', shell=True)
    proc = subprocess.Popen(
        'sudo /usr/bin/python /home/pi/lightshowpi/py/synchronized_lights.py --file="/home/pi/lightshowpi/music/sb_party_switch.mp3"', shell=True)
    # light show pi takes some time to start up
    # asyncio.run(asyncio.sleep(3))
    loop = asyncio.get_event_loop()
    # loop.run_until_complete(asyncio.sleep(3))

    wave = asyncio.create_task(mv.wave())
    look = asyncio.create_task(mv.lookAround())
    loop.run_until_complete(wave)
    loop.run_until_complete(look)
    # asyncio.run(mv.wave())
    # asyncio.run(mv.lookAround())
    proc.terminate()


def welcome():
    # works - copy from this - don't change
    mv = Movements("Orchestrate Movements")
    proc = subprocess.Popen(
        'sudo /usr/bin/python /home/pi/lightshowpi/py/synchronized_lights.py --file="/home/pi/lightshowpi/music/sb_party_switch.mp3"', shell=True)
    # light show pi takes some time to start up
    loop = asyncio.get_event_loop()
    loop.run_until_complete(asyncio.sleep(3))
    asyncio.run(mv.lookAround())
    proc.terminate()


def oldMain():
    print("starting")

    mv = Movements("Orchestrate Movements")
    #subprocess.call('sudo /usr/bin/python /home/pi/lightshowpi/py/synchronized_lights.py --file="/home/pi/lightshowpi/music/sb_party_switch.mp3"', shell=True)
    proc = subprocess.Popen(
        'sudo /usr/bin/python /home/pi/lightshowpi/py/synchronized_lights.py --file="/home/pi/lightshowpi/music/sb_party_switch.mp3"', shell=True)
    # light show pi takes some time to start up
    # asyncio.run(asyncio.sleep(3))
    loop = asyncio.get_event_loop()
    loop.run_until_complete(asyncio.sleep(3))
    # wave = asyncio.create_task(mv.wave())
    # look = asyncio.create_task(mv.lookAround())
    # loop.run_until_complete(wave)
    # loop.run_until_complete(look)
    # RuntimeError: no running event loop
    # asyncio.run(mv.wave())
    # can't use  asyncio.run more than once so need another way. if we use await this requires method to be async
    # and we run into issues with calling asyncio.run() a second time. have to find another way
    # asyncio.run(main()) # can't use this because of other event loops runnign in code
# RuntimeError: asyncio.run() cannot be called from a running event loop
# sys:1: RuntimeWarning: coroutine 'TrunkController.neckCenter' was never awaited
    asyncio.run(mv.lookAround())
    proc.terminate()

    #loop = asyncio.get_event_loop()
    # wave = loop.create_task(mv.wave())
    # play = loop.create_task(Lightshow.main())
    #wave = asyncio.create_task(mv.wave())
    #play = asyncio.create_task(Lightshow.main())
    # await wave
    # await play
    #loop.run_until_complete(asyncio.gather(play, wave))


# works
async def move():
    await asyncio.sleep(2.75)
    mv = Movements("Orchestrate Movements")
    wave = asyncio.create_task(mv.wave())
    swivelHead = asyncio.create_task(mv.swivelHead())
    await asyncio.gather(wave, swivelHead)
    
def startParty():
    # works - copy from this - don't change
    #mv = Movements("Orchestrate Movements")
    proc = subprocess.Popen(
        'sudo /usr/bin/python /home/pi/lightshowpi/py/synchronized_lights.py --file="/home/pi/lightshowpi/music/sb_party_switch.mp3"', shell=True)

    # light show pi takes some time to start up
    asyncio.run(move())
    #loop = asyncio.get_event_loop()
    #loop = asyncio.new_event_loop()
    #asyncio.set_event_loop(loop)
    #loop.run_until_complete(asyncio.sleep(.1))
    #wave = asyncio.create_task(mv.wave())
    #lookAround = asyncio.create_task(mv.lookAround())
    # loop = asyncio.new_event_loop()
    # asyncio.set_event_loop(loop)
    # loop.run_until_complete(asyncio.gather(mv.wave(), mv.lookAround()))

    proc.terminate()


def main():
    startParty()


main()
# asyncio.run(main()) # can't use this because of other event loops runnign in code
# RuntimeError: asyncio.run() cannot be called from a running event loop
# sys:1: RuntimeWarning: coroutine 'TrunkController.neckCenter' was never awaited

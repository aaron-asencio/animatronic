from movements import Movements
import asyncio
import concurrent.futures
import threading
import subprocess
from subprocess import Popen
"""

To put in input mode - this actually started playing xmas playlist - have to look into that
sudo /usr/bin/python /home/pi/lightshowpi/py/animatronic.py --config="/home/pi/lightshowpi/config/overrides-mic.cfg"

"""
class Animatronic:
    #def __init__(self):

    
    lightshowPlayCmd = 'sudo /usr/bin/python /home/pi/lightshowpi/py/synchronized_lights.py --file='
    lightshowPlayFile1 = '"/home/pi/lightshowpi/music/sb_party_switch.mp3"'
    lightshowPlayFile2 = '"/home/pi/lightshowpi/music/sb_ripped_pants.mp3"'
    lightshowDir = '/home/pi/lightshowpi/music/'


    def welcome(self):
        # works - copy from this - don't change
        mv = Movements("Orchestrate Movements")
        proc = subprocess.Popen(
            'sudo /usr/bin/python /home/pi/lightshowpi/py/synchronized_lights.py --file="/home/pi/lightshowpi/music/sb_party_switch.mp3"', shell=True)
        # light show pi takes some time to start up
        loop = asyncio.get_event_loop()
        loop.run_until_complete(asyncio.sleep(3))
        asyncio.run(mv.lookAround())
        proc.terminate()

    # works
    # strategy - put all of the async methods in one async function so we can call it with asyncio.run only once
    async def swivelHeadAndWave(self):
        mv = Movements("Orchestrate Movements")
        # light show pi takes some time to start up
        await asyncio.sleep(2)
        wave = asyncio.create_task(mv.wave())
        swivelHead = asyncio.create_task(mv.swivelHead())
        await asyncio.gather(wave, swivelHead)


    async def comeAndSwivelHead(self):
        mv = Movements("Orchestrate Movements")
        # light show pi takes some time to start up
        await asyncio.sleep(2.75)
        wave = asyncio.create_task(mv.come())
        swivelHead = asyncio.create_task(mv.swivelHead())
        await asyncio.gather(wave, swivelHead)


    def startParty(self):
        # works - copy from this - don't change
        cmd = self.lightshowPlayCmd + self.lightshowPlayFile1
        proc = subprocess.Popen(cmd, shell=True)
        asyncio.run(self.swivelHeadAndWave())
        proc.terminate()


    # generalize this so we can specify audio file and action only when calling - just use locals()["swivelHeadAndWave"]()
    # take function name and audio file
    def runMethod(self):
        # works - copy from this - don't change
        asyncio.run(getattr(self, "swivelHeadAndWave")())
        cmd = self.lightshowPlayCmd + self.lightshowPlayFile1
        proc = subprocess.Popen(cmd, shell=True)
        proc.terminate()

    # take function name and audio file
    def runActionAndAudio(self, methodName, audioFile):
        # works - copy from this - don't change
        #asyncio.run(getattr(self, methodName)())
        cmd = self.lightshowPlayCmd + '"' + self.lightshowDir + audioFile + '"'
        print(cmd)
        proc = subprocess.Popen(cmd, shell=True)
        proc.terminate()

    def rippedPants(self):
        # works - copy from this - don't change
        cmd = self.lightshowPlayCmd + self.lightshowPlayFile1
        proc = subprocess.Popen(cmd, shell=True)
        asyncio.run(self.swivelHeadAndWave())
        proc.terminate()


    def main(self):
        # startParty()
        #self.runMethod()
        self.runActionAndAudio("swivelHeadAndWave", "sb_party_switch.mp3")
        

a = Animatronic()
a.main()
# asyncio.run(main()) # can't use this because of other event loops runnign in code
# RuntimeError: asyncio.run() cannot be called from a running event loop
# sys:1: RuntimeWarning: coroutine 'TrunkController.neckCenter' was never awaited

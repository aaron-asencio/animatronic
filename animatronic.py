from movements import Movements
import asyncio
import concurrent.futures
import threading
import subprocess
from subprocess import Popen
"""
command to run lightshowpi and play a music file
sudo /usr/bin/python /home/pi/lightshowpi/py/synchronized_lights.py --file="/home/pi/animatronic/music/sb_party_switch.mp3"

To put in input mode - this actually started playing xmas playlist - have to look into that
sudo /usr/bin/python /home/pi/lightshowpi/py/synchronized_lights.py --config="/home/pi/lightshowpi/config/overrides-mic.cfg"

"""


class Animatronic:
    # def __init__(self):
    
    # 
    lightshowPlayCmd = 'sudo /usr/bin/python /home/pi/lightshowpi/py/synchronized_lights.py --file='
    #lightshowDir = '/home/pi/lightshowpi/music/'
    lightshowDir = '/home/pi/animatronic/music/'
    
    #list of files on server
    music = ['beetel-exorcist.wav', 'blah.wav', 'bloody-mary.mp3', 'chucky.mp3', 'yoda-fear.wav', 'krusty-laugh.wav', 'sb_party_switch.wav', 'sb_ripped_pants.mp3',
             'spongebob-torture.mp3', 'vader-beaten.wav', 'vader-father.wav', 'were-waiting.wav', 'yoda-900.wav', 'yoda-agent-evil.wav']

    idle = 3

    # strategy - put all of the async methods in one async function so we can call it with asyncio.run only once
    async def patrol(self):
        mv = Movements()
        await asyncio.sleep(self.idle)
        await mv.neckEllipse()
        await asyncio.sleep(1)
        await asyncio.sleep(1)
        await mv.lookAroundSmall()

    async def swivelHeadAndWave(self):
        mv = Movements()
        await asyncio.sleep(self.idle)
        wave = asyncio.create_task(mv.wave())
        swivelHead = asyncio.create_task(mv.swivelHead())
        await asyncio.gather(wave, swivelHead)    
    
    async def no(self):
        mv = Movements()
        await asyncio.sleep(self.idle)
        await mv.shakeNo()
      
    async def comeAndSwivelHead(self):
        mv = Movements()
        await asyncio.sleep(self.idle)
        action1 = asyncio.create_task(mv.come())
        action2 = asyncio.create_task(mv.swivelHead())
        await asyncio.gather(action1, action2)
    
    async def comeAndLook(self):
        mv = Movements()
        await asyncio.sleep(self.idle)
        wave = asyncio.create_task(mv.come())
        swivelHead = asyncio.create_task(mv.lookAround())
        await asyncio.gather(wave, swivelHead)

    async def comeinLookAroundSmall(self):
        mv = Movements()
        await asyncio.sleep(self.idle)
        action1 = asyncio.create_task(mv.comein())
        action2 = asyncio.create_task(mv.lookAroundSmall())
        await asyncio.gather(action1, action2)
    
    async def armOutLookAroundSmall(self):
        mv = Movements()
        await asyncio.sleep(self.idle)
        action1 = asyncio.create_task(mv.armOut())
        action2 = asyncio.create_task(mv.lookAroundSmall())
        await asyncio.gather(action1, action2)

    # take function name and audio file
    def runActionAndAudio(self, methodName, audioFile):
        # works - copy from this - don't change
        cmd = self.lightshowPlayCmd + '"' + self.lightshowDir + audioFile + '"'
        print(cmd)
        proc = subprocess.Popen(cmd, shell=True)
        asyncio.run(getattr(self, methodName)())
        proc.terminate()

    def audioTest(self):
        cmd = 'sudo /usr/bin/python /home/pi/lightshowpi/py/synchronized_lights.py --file="/home/pi/animatronic/music/blah.wav"'
        proc = subprocess.Popen(cmd, shell=True)
        
        while proc.poll() is None:
            # do nothing
           x=0
        proc.terminate()
    
    def startParty(self):
       self.runActionAndAudio("swivelHeadAndWave", self.music[6])

    def rippedPants(self):
        self.runActionAndAudio("comeAndLook", self.music[7])

    def exorcist(self):
        self.runActionAndAudio("comeAndLook", self.music[0])    

    def yoda900(self):
        self.runActionAndAudio("patrol", self.music[12])

    def blah(self):
       self.runActionAndAudio("no", self.music[1])

    def vaderBeaten(self):
        self.runActionAndAudio("patrol", self.music[9])
    
    def waiting(self):
        self.runActionAndAudio("comeAndLook", self.music[11])
    
    def vaderFather(self):
        self.runActionAndAudio("comeAndLook", self.music[10])

    def bloody(self):
        self.runActionAndAudio("comeAndLook", self.music[2]) 
    
    def yodaFear(self):
        self.runActionAndAudio("armOutLookAroundSmall", self.music[4]) 
            
      
    #def main(self):
        #self.startParty()
        #self.yoda900()
        #self.rippedPants()
        #self.blah()
        #self.vaderBeaten()
        #self.exorcist()
        #self.waiting()
        #self.vaderFather()
        #self.bloody()
        #self.yodaFear()


#a = Animatronic()
#a.main()
# asyncio.run(main()) # can't use this because of other event loops runnign in code
# RuntimeError: asyncio.run() cannot be called from a running event loop
# sys:1: RuntimeWarning: coroutine 'TrunkController.neckCenter' was never awaited

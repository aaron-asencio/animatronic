from movements import Movements
import asyncio
import concurrent.futures
import threading
import subprocess
from subprocess import Popen
import argparse

"""
Be careful using ~ for shortcut to /home/pi esp. with LSP
sudo /usr/bin/python3 /home/pi/workspace/animatronic/animatronic.py --action="startParty"

sudo /usr/bin/python3 /home/pi/workspace/lightshowpi/py/synchronized_lights.py --file="/home/pi/Music/sb_party_switch.wav"

To put in input mode - overrides config file must be in lightshowpi/config. Using full path will result in LSP ignoring the file and using default.
sudo /usr/bin/python3 /home/pi/workspace/lightshowpi/py/synchronized_lights.py --config="overrides-mic.cfg"

"""


class Animatronic:
    # def __init__(self):

    lightshowPlayCmd = 'sudo /usr/bin/python /home/pi/workspace/lightshowpi/py/synchronized_lights.py --file='
    lightshowPlayFile1 = '"~/Music/sb_party_switch.wav"'
    lightshowPlayFile2 = '"~/Music/blah.wav"'
    lightshowDir = '/home/pi/Music/'

    music = ['beetel-exorcist.wav', 'blah.wav', 'krusty-laugh.wav', 'sb_party_switch.wav','spongebob-torture.mp3',
             'vader-beaten.wav', 'vader-father.wav', 'were-waiting.wav', 'yoda-900.wav', 'yoda-agent-evil.wav',
             'yoda-fear.wav','hello-everyone.wav','happy-halloween.wav','walk.wav','how-yall.wav','cant-hear.wav']

    idle = 3

    # strategy - put all of the async methods in one async function so we can call it with asyncio.run only once
    async def patrol(self):
        mv = Movements("Orchestrate Movements")
        await asyncio.sleep(self.idle)
        await mv.neckEllipse()
        await asyncio.sleep(1)
        await asyncio.sleep(1)
        await mv.lookAroundSmall()


    async def swivelHeadAndWave(self):
        mv = Movements("Orchestrate Movements")
        await asyncio.sleep(self.idle)
        wave = asyncio.create_task(mv.wave())
        swivelHead = asyncio.create_task(mv.swivelHead())
        await asyncio.gather(wave, swivelHead)    
    
    async def no(self):
        mv = Movements("Orchestrate Movements")
        await asyncio.sleep(self.idle)
        await mv.shakeNo()

    async def wave(self):
        mv = Movements("Orchestrate Movements")
        await asyncio.sleep(self.idle)
        await mv.wave()

    async def lookAroundSmall(self):
        mv = Movements("Orchestrate Movements")
        await asyncio.sleep(1)
        await mv.lookAroundSmall()
      
    async def neckEllipse(self):
        mv = Movements("Orchestrate Movements")
        await asyncio.sleep(1)
        await mv.neckEllipse()

    async def comeAndSwivelHead(self):
        mv = Movements("Orchestrate Movements")
        await asyncio.sleep(self.idle)
        wave = asyncio.create_task(mv.come())
        swivelHead = asyncio.create_task(mv.swivelHead())
        await asyncio.gather(wave, swivelHead)
    
    async def comeAndLook(self):
        mv = Movements("Orchestrate Movements")
        await asyncio.sleep(self.idle)
        wave = asyncio.create_task(mv.come())
        swivelHead = asyncio.create_task(mv.lookAround())
        await asyncio.gather(wave, swivelHead)

    # take function name and audio file
    def runActionAndAudio(self, methodName, audioFile):
        # works - copy from this - don't change
        cmd = self.lightshowPlayCmd + '"' + self.lightshowDir + audioFile + '"'
        print(cmd)
        proc = subprocess.Popen(cmd, shell=True)
        asyncio.run(getattr(self, methodName)())
        proc.terminate()

    def startParty(self):
       self.runActionAndAudio("swivelHeadAndWave", self.music[3])

    def rippedPants(self):
        self.runActionAndAudio("comeAndLook", self.music[4])

    def torture(self):
        self.runActionAndAudio("comeAndLook", self.music[4])    

    def exorcist(self):
        self.runActionAndAudio("comeAndLook", self.music[0])    

    def yoda900(self):
        self.runActionAndAudio("patrol", self.music[9])

  
    def vaderBeaten(self):
        self.runActionAndAudio("patrol", self.music[5])
    
    def waiting(self):
        self.runActionAndAudio("comeAndLook", self.music[7])
    
    def vaderFather(self):
        self.runActionAndAudio("comeAndLook", self.music[6])

    def krusty(self):
        self.runActionAndAudio("neckEllipse", self.music[2]) 
    
    def yodaFear(self):
        self.runActionAndAudio("comeAndLook", self.music[10]) 
 
    def blah(self):
       self.runActionAndAudio("no", self.music[1])
        
    def hello(self):
        self.runActionAndAudio("wave", self.music[11])
        print("calling hello")
 
    def happyHalloween(self):
        self.runActionAndAudio("wave", self.music[12])
    
    def niceDay(self):
        self.runActionAndAudio("wave", self.music[13])
   
    def howYallDoin(self):
        self.runActionAndAudio("wave", self.music[14])
    
    def cantHear(self):
        self.runActionAndAudio("wave", self.music[15])  
    
def main(arg):
    #self.startParty() # pretty good
    #self.yoda900() # doesn't move jaw much
    #self.rippedPants() # need different motion
    #self.torture() # need different motion
    # self.blah() # need to shorten the delay depending on audio work on more custom motions here
    #self.vaderBeaten()
    #self.exorcist()
    #self.waiting()
    #self.vaderFather()
    #self.krusty()
   
    a = Animatronic()
        
    if (args.action == 'startParty'):
        a.startParty()
    elif(args.action == 'yoda'):
        a.yoda900()
    #elif(args.action == 'rippedPants'): # playing torture - need audio
    #    a.rippedPants()
    elif(args.action == 'torture'):
        a.torture()   
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
    elif(args.action == 'krusty'):
        a.krusty()
    elif(args.action == 'hello'):
        a.hello()
    elif(args.action == 'happyHalloween'):
        a.happyHalloween()
    elif(args.action == 'niceDay'):
        a.niceDay()
    elif(args.action == 'howYallDoin'):
        a.howYallDoin()
    elif(args.action == 'cantHear'):
        a.cantHear()    
    
    elif(args.action == 'mic'):
        #a.lookAroundSmall() 
        cmd = 'sudo /usr/bin/python /home/pi/workspace/lightshowpi/py/synchronized_lights.py --config /home/pi/workspace/lightshowpi/config/overrides-mic.cfg'
        print(cmd)
        proc = subprocess.Popen(cmd, shell=True)
        # run an action to go with audio
        #asyncio.run(getattr(self, methodName)())
        proc.terminate()

# asyncio.run(main()) # can't use this because of other event loops runnign in code
# RuntimeError: asyncio.run() cannot be called from a running event loop
# sys:1: RuntimeWarning: coroutine 'TrunkController.neckCenter' was never awaited
# sudo /usr/bin/python ~/workspace/animatronic/animatronic.py --action=startParty
# Arguments
#a.main()
#if __name__ == "__main__":
parser = argparse.ArgumentParser()
parser.add_argument('--action', default=None, help='Action to perform.')
args = parser.parse_args()
print(args.action)
main(args)

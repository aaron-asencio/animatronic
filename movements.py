from time import sleep
import asyncio
from trunkcontroller import TrunkController
import concurrent.futures

"""
Macro movements built from trunkcontroller functions
"""


class Movements:
    def __init__(self, name):
        self.name = name

    def lookAround(self):
        loop = asyncio.get_event_loop()
        controller = TrunkController("Servo TrunkController")
        neckTilt = loop.create_task(controller.neckTilt())
        neckPan = loop.create_task(controller.neckPan())
        shoulder = loop.create_task(controller.shoulder())
        loop.run_until_complete(asyncio.gather(neckTilt, neckPan, shoulder))
        asyncio.run(controller.neckCenter())

    def scan(self):
        controller = TrunkController("Servo TrunkController")
        for i in range(0, 2):
            asyncio.run(controller.neckPan())

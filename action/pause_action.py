from time import sleep

class Pause:
    def __init__(self, seconds):
        self.seconds = seconds

    def do(self):
        print("Pausing  for ", self.seconds, " seconds.")
        sleep(self.seconds)
       
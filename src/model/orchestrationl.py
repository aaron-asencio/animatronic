from queue import Queue

class Orchestration:

    # constructor
    def __init__(self):
        self.queue = Queue()
             

    def run():
        """ Orchestration  """
        # get the next bodypart from the queue
        # get the next position from the bodypart
        # move the bodypart to the next position
        # repeat until the queue is empty
        while not self.queue.empty():
            bodypart = self.queue.get()
            # body_part_postion class that has bodypart and the position it is moving to and from
            bodypart.move()
            self.queue.task_done()
       
    
    def add(self, bodypart):
        self.queue.put(bodypart)

    def remove(self, bodypart):
        self.queue.remove(bodypart)


# hold map of positions of servos thqt will run at the same time

#  array of postion objects. servos ojects have servo number, name, description, default pos , start position and end position, deay between each position,
#  could have action instead - move, wait, stop, etc


# could do orchestration type like parallel or sequence

# orchestration needs to know the body annd where it is moving to and from



"""
Class to store everything a body part needs to move.
BodyPart have servo number, name, description, default pos , start position and end position, delay between each position
"""
class BodyPart:
    def __init__(self, name, servo, default_pos, current_pos=0):
        self.name = name
        self.servo = servo
        self.default_pos = default_pos
        self.current_pos = current_pos
        

    def __str__(self):
        return f"{self.name} {self.servo} {self.default_pos}"
    
    def move(self, start_pos, end_pos, delay):
        print(f"Moving {self.name} to {end_pos} from {start_pos} with delay {delay}")
    
    
    



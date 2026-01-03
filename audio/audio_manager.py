import os

def play_beep():
    os.system("aplay /usr/share/sounds/alsa/Front_Center.wav 2>/dev/null")

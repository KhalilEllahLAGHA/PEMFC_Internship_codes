
#import matplotlib.pyplot as plt
'''
import matplotlib
matplotlib.use("Qt5Agg")
plt = matplotlib.pyplot
'''
'''
 supported values are ['GTK3Agg', 'GTK3Cairo', 'GTK4Agg', 'GTK4Cairo', 'MacOSX', 'nbAgg', 'QtAgg', 'QtCairo', 'Qt5Agg', 'Qt5Cairo', 'TkAgg', 'TkCairo', 'WebAgg', 'WX', 'WXAgg', 'WXCairo', 'agg', 'cairo', 'pdf', 'pgf', 'ps', 'svg', 'template']

'''

from collections import deque
from typing import Deque, Optional

# Rolling display window length (number of samples kept for live plotting).
# Was the magic number `shift_img = 180` in main_gui0.shift_image().
DISPLAY_BUFFER_LEN: int = 180


class Sensor_PEMstack():
 """
 classe définissant un capteur caractérisé par:
 - son nom
 - son type (Pressure,  flow, current,voltage)

 Display buffers are bounded deques (maxlen=DISPLAY_BUFFER_LEN): appending
 beyond the window automatically drops the oldest sample in O(1), which
 replaces the old list + pop(0) trimming done by shift_image().
 """
 def __init__(self, name_sensor: str = "sensor00", id_sensor="A00",
              type_sensor: str = "unknow", value_sensor: float = 255,
              Xcoordinate: float = 0,
              display_len: int = DISPLAY_BUFFER_LEN) -> None:
   # super(Sensor_PEMstack, self).__init__()
    self.nom          = name_sensor
    self.idsensor     = id_sensor
    self.kindsensor   = type_sensor
    self.array_conv_v: Deque[float] = deque([value_sensor], maxlen=display_len)
    self.abscisses:    Deque[float] = deque([Xcoordinate], maxlen=display_len)

 def changeNom(self, nouveau_nom: str) -> None:
    self.nom = nouveau_nom

 def changeid(self, nouveau_id) -> None:
    self.idsensor = nouveau_id

 def changekind(self, nouveau_type: str) -> None:
    self.kindsensor = nouveau_type

 def set_array_convertion(self, value_sensor: float) -> None:
    self.array_conv_v.append(value_sensor)

 def clean_array_convertion(self) -> None:
    # kept for API compatibility; guarded so an empty buffer cannot raise
    if self.array_conv_v:
        self.array_conv_v.popleft()

 def clean_abscissas(self) -> None:
     if self.abscisses:
         self.abscisses.popleft()

 def clear_all(self) -> None:
    """Empty both buffers (used by the New Experiment reset)."""
    self.array_conv_v.clear()
    self.abscisses.clear()

 def setV_X(self, Xcoordinate: float) -> None:
    self.abscisses.append(Xcoordinate)
    # self.abscisses= range(len(self.array_conv_v))

 def read_name(self) -> str:
    return (self.nom)

 def read_id(self):
    return (self.idsensor)

 def read_kind(self) -> str:
    return (self.kindsensor)

 def read_arrayconv(self) -> Deque[float]:
    return (self.array_conv_v)

 def read_abscisses(self) -> Deque[float]:
    return (self.abscisses)


 def plot_signal(self) -> None:
    # matplotlib is imported lazily: the module-level import is commented out
    # above, and importing it here keeps the GUI start-up light. The original
    # code referenced a `plt` name that no longer existed (NameError).
    import matplotlib.pyplot as plt
    plt.close()
    #print("x={} |||| y={}".format(self.abscisses,self.array_conv_v))
    plt.title('sensor: ' + self.nom)
    plt.ylabel('Amplitud: ' + self.kindsensor)
    plt.xlabel('Points')
    plt.grid()
    plt.plot(self.abscisses,self.array_conv_v,"-*b",markersize=3, label=self.nom)
    plt.show()
'''
def Nsensor(self,id_sensor):
   if id_sensor== 0: return 'Cell0'
   elif id_sensor== 1: return 'Cell1'
   elif id_sensor== 2: return 'Cell2'
   elif id_sensor== 3: return 'Cell3'
   elif id_sensor== 4: return 'Cell4'
   elif id_sensor==  5:return 'Cell5'
   elif id_sensor==  6:return 'Cell6'
   elif id_sensor==   7:return 'Cell7'
   elif id_sensor==   8:return 'Cell8'
   elif id_sensor==   9:return 'Cell9'
   elif id_sensor==   10:return 'Psensor10'
   elif id_sensor==   11:return 'Psensor11'
   elif id_sensor==   12:return 'Psensor12'
   elif id_sensor==   13:return 'Isensor13'
   elif id_sensor==    14:return 'M_Fsensor14'
   elif id_sensor==  15:return 'M_Fsensor15'
   else   :return 'Unknow Sensor'
'''

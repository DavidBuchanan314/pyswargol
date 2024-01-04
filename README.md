# pyswargol

```
$ python3 swargol.py --help
Usage: swargol.py [OPTIONS]

Render Conway's Game of Life via SDL2, unreasonably quickly.

Options:
  --width=INT          framebuffer width (default: 1280)
  --height=INT         framebuffer height (default: 720)
  --vsync=BOOL         enable vsync (default: True)
  --fullscreen         enable fullscreen
  --drylife=BOOL       use the non-standard "drylife" algorithm (default: True)
  --slow               use the very slow implementation (for benchmark comparisons)
  --frameskip=INT      only render 1-in-n frames to the screen (default: 1)
  --num-procs=INT      degree of parallelism (NB: number of actual threads will be 2n+1) (default: 8)
  --bench-frames=INT   render a certain number of frames and then exit (default: 0)

Other actions:
  -h, --help           Show the help
```

![image](https://github.com/DavidBuchanan314/pyswargol/assets/13520633/217eaf38-d8b6-43ef-a37a-98a229dcae31)

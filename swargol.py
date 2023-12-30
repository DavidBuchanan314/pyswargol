import sdl2
import os
import time
from queue import Queue, Empty
from threading import Thread
from multiprocessing import Pipe, Process, Event
from typing import List
from dataclasses import dataclass

"""

┌───────────┐             Graphics Process
│ ┌────┐    │    ┌───────────────────────────────┐
│ │  ┌─▼────┴─┐  │  ┌─────────┐  ┌────────────┐  │
│ │  │  Life  ├─────► Blitter ├──►            │  │
│ │  └─┬────▲─┘  │  └─────────┘  │            │  │
▼ │  ┌─▼────┴─┐  │  ┌─────────┐  │            │  │
│ ▲  │  Life  ├─────► Blitter ├──►            │  │
│ │  └─┬────▲─┘  │  └─────────┘  │    GUI     │  │
│ │  ┌─▼────┴─┐  │  ┌─────────┐  │  Renderer  │  │
│ │  │  Life  ├─────► Blitter ├──►            │  │
│ │  └─┬────▲─┘  │  └─────────┘  │            │  │
▼ │  ┌─▼────┴─┐  │  ┌─────────┐  │            │  │
│ ▲  │  Life  ├─────► Blitter ├──►            │  │
│ │  └─┬────▲─┘  │  └─────────┘  └────────────┘  │
│ └────┘    │    └───────────────────────────────┘
└───────────┘

"Life" threads implement the SWAR life algorithm, for a horizontal strip of
the overall canvas.

Blitter threads use SDL2 functions to unpack the SWAR buffers into RGBA8888
surfaces, which are passed to the main Renderer thread.

The renderer thread is responsible for uploading the surfaces to a GPU texture,
and making it show up on the screen. It's also responsible for dealing with SDL
events.

Each Life thread lives in its own process, to avoid the GIL. They
talk to each other (for overlap/wraparound), and to the Blitter threads (to
report their results), using Pipes.

Everything else happens in the main process, so the Blitters can talk to the
main thread using standard Queues - the hard work here is done inside SDL2,
which does not hold the GIL (meaning we can use multithreading, as opposed
to multiprocessing).

"""

# I prefer the way scaling works by default on x11, and can't be bothered to fix it under wayland
os.environ["SDL_VIDEODRIVER"] = "x11"

SURFACE_FMT = sdl2.SDL_PIXELFORMAT_ARGB8888

# at time of writing, SDL2 blits INDEX4LSB surfaces as if they were actually INDEX4MSB
# Setting this to False should give a small perf boost, but will render incorrectly on
# not-bleeding-edge SDL2 builds
INDEX4LSB_WORKAROUND = True
WIDTH_PADDING = 16
GLIDER_TEST = False

# RGBA
COLOUR_OFF = (40,  40,  40,  255)
COLOUR_ON  = (255, 255, 255, 255)


@dataclass(kw_only=True)
class LifeConfig:
	"""
	Render Conway's Game of Life via SDL2, unreasonably quickly.

	:param width: framebuffer width
	:param height: framebuffer height
	:param vsync: enable vsync
	:param fullscreen: enable fullscreen
	:param drylife: use the non-standard "drylife" algorithm
	:param frameskip: only render 1-in-n frames to the screen
	:param num_procs: degree of parallelism (NB: number of actual threads will be 2n+1)
	""" # this docstring is used by clize

	width:        int = 1280
	height:       int = 720
	vsync:        bool = True
	fullscreen:   bool = False
	drylife:      bool = True
	frameskip:     int = 1
	num_procs:    int  = 8


# return all remaining items in a queue
def queue_purge(queue: Queue):
	try:
		while True:
			yield queue.get_nowait()
	except Empty:
		pass


def life_thread(cfg: LifeConfig, i, width, height, packed_pipe, pipe_top, pipe_bottom):
	STRIDE = width + WIDTH_PADDING
	STATE_BYTE_LENGTH = (STRIDE * height) // 2
	COLSHIFT = STRIDE * 4
	WRAPSHIFT = STRIDE * height * 4
	BIAS = (STRIDE + 2) * 4

	MASK_1 = int.from_bytes(b"\x11" * STATE_BYTE_LENGTH, "little") << BIAS
	MASK_CANVAS = int.from_bytes((b"\x11" * (width // 2) + b"\x00" * (WIDTH_PADDING // 2)) * height, "little") << BIAS
	MASK_WRAP_LEFT = int.from_bytes((b"\x11" * ((WIDTH_PADDING // 2) // 2) + b"\x00" * ((width - WIDTH_PADDING // 2) // 2) + b"\x00" * (WIDTH_PADDING // 2)) * (height + 2), "little") << (2 * 4)
	MASK_WRAP_RIGHT = int.from_bytes((b"\x00" * ((width - WIDTH_PADDING // 2) // 2) + b"\x11" * ((WIDTH_PADDING // 2) // 2) + b"\x00" * (WIDTH_PADDING // 2)) * (height + 2), "little") << (2 * 4)
	MASK_NOT_3 = MASK_1 * (15 ^ 3)
	MASK_NOT_4 = MASK_1 * (15 ^ 4)
	MASK_NOT_7 = MASK_1 * (15 ^ 7)

	if not GLIDER_TEST:
		seed_bytes = os.urandom(STATE_BYTE_LENGTH)
	else:
		# glider test
		seed_bytes = bytearray(STATE_BYTE_LENGTH)
		if i == 0:
			seed_bytes[(STRIDE//2)*4+3:(STRIDE//2)*4+3+2] = b"\x10\x00"
			seed_bytes[(STRIDE//2)*5+3:(STRIDE//2)*5+3+2] = b"\x00\x01"
			seed_bytes[(STRIDE//2)*6+3:(STRIDE//2)*6+3+2] = b"\x11\x01"

	state = (int.from_bytes(seed_bytes, "little") << BIAS) & MASK_CANVAS
	pipe_top.send_bytes(seed_bytes[:STRIDE//2]) # send up our top row
	pipe_bottom.send_bytes(seed_bytes[-STRIDE//2:]) # send down our bottom row

	framectr = 0
	try:
		while True: # we'll keep going until killed
			"""
			if we include ourself as a neighbor:
			alive = (exactly 3 neighbors) or (alive and 4 neighbors)
			"""

			# implement wraparound
			# vertical wrap
			state |= int.from_bytes(pipe_top.recv_bytes(), "little") | (int.from_bytes(pipe_bottom.recv_bytes(), "little") << (WRAPSHIFT + BIAS))
			# horizontal wrap
			state |= ((state & MASK_WRAP_LEFT) << (width * 4)) | ((state & MASK_WRAP_RIGHT) >> (width * 4))

			# count neighbors
			summed = state
			summed += (summed >> 4) + (summed << 4)
			summed += (summed >> COLSHIFT) + (summed << COLSHIFT)

			# check if there are exactly 3 neighbors
			has_3_neighbors = summed ^ MASK_NOT_3 # at this point, a value of all 1s means it was initially 3
			has_3_neighbors &= has_3_neighbors >> 2 # fold in half
			has_3_neighbors &= has_3_neighbors >> 1 # fold in half again
			
			# check if there are exactly 4 neighbors
			has_4_neighbors = summed ^ MASK_NOT_4 # at this point, a value of all 1s means it was initially 4
			has_4_neighbors &= has_4_neighbors >> 2  # fold in half
			has_4_neighbors &= has_4_neighbors >> 1  # fold in half again

			if cfg.drylife:
				# check if there are exactly 7 neighbors
				has_7_neighbors = summed ^ MASK_NOT_7 # at this point, a value of all 1s means it was initially 7
				has_7_neighbors &= has_7_neighbors >> 2  # fold in half
				has_7_neighbors &= has_7_neighbors >> 1  # fold in half again
				
				# variable names here are misleading...
				has_7_neighbors &= ~state
				has_3_neighbors |= has_7_neighbors

			# apply game-of-life rules
			state &= has_4_neighbors
			state |= has_3_neighbors
			state &= MASK_CANVAS

			packed_state = (state>>BIAS).to_bytes(STATE_BYTE_LENGTH, "little")
			pipe_top.send_bytes(packed_state[:STRIDE//2+1])
			pipe_bottom.send_bytes(packed_state[-(STRIDE//2+1):])

			framectr += 1
			if framectr % cfg.frameskip:
				continue

			packed_pipe.send_bytes(packed_state[-WIDTH_PADDING//2::-1] if INDEX4LSB_WORKAROUND else packed_state)

	except KeyboardInterrupt: # this should only happen if the user pressed Ctrl+C
		print("life_thread SIGINT")
		while True:
			packed_pipe.send_bytes(bytes(STATE_BYTE_LENGTH)) # unblock any readers, until we get killed


def blit_thread(cfg: LifeConfig, i: int, section_height :int, stopped: Event, packed_queue, blitted_queue: Queue):
	while not stopped.is_set():
		packed_frame = packed_queue.recv_bytes() # this needs to stay in scope until SDL_ConvertSurfaceFormat is complete!
		surface = sdl2.SDL_CreateRGBSurfaceWithFormatFrom(
			packed_frame,
			cfg.width, section_height,
			4, # bit-depth
			(cfg.width + WIDTH_PADDING) // 2, # pitch
			sdl2.SDL_PIXELFORMAT_INDEX4MSB if INDEX4LSB_WORKAROUND else sdl2.SDL_PIXELFORMAT_INDEX4LSB
		)
		sdl2.SDL_SetPaletteColors(surface.contents.format.contents.palette, sdl2.SDL_Color(*COLOUR_OFF), 0, 1)
		sdl2.SDL_SetPaletteColors(surface.contents.format.contents.palette, sdl2.SDL_Color(*COLOUR_ON), 1, 1)
		blitted_surface = sdl2.SDL_ConvertSurfaceFormat(surface, SURFACE_FMT, 0)
		if not blitted_surface:
			raise Exception("SDL_ConvertSurfaceFormat: " + sdl2.SDL_GetError().decode())
		blitted_queue.put(blitted_surface)
		sdl2.SDL_FreeSurface(surface)
	
	print(f"blit_thread {i}: graceful exit")
	packed_queue.recv_bytes() # do a final read to un-block the writer
	packed_queue.close() # close our end of the Pipe


def gui_thread(cfg: LifeConfig, section_heights: List[int], blitted_queues: List[Queue]):
	window = sdl2.SDL_CreateWindow(
		b"pysdl2 framebuffer test",
		sdl2.SDL_WINDOWPOS_UNDEFINED, sdl2.SDL_WINDOWPOS_UNDEFINED,
		cfg.width, cfg.height,
		sdl2.SDL_WINDOW_SHOWN
	)

	if not window:
		raise Exception("Failed to create SDL2 Window")

	renderer = sdl2.SDL_CreateRenderer(window, -1, sdl2.SDL_RENDERER_ACCELERATED | (sdl2.SDL_RENDERER_PRESENTVSYNC if cfg.vsync else 0))

	if not renderer:
		raise Exception("Failed to create SDL2 Renderer")

	if cfg.fullscreen:
		sdl2.SDL_SetWindowFullscreen(window, sdl2.SDL_WINDOW_FULLSCREEN)
		sdl2.SDL_ShowCursor(sdl2.SDL_DISABLE) # hide cursor

	textures = []
	for h in section_heights:
		texture = sdl2.SDL_CreateTexture(
			renderer,
			SURFACE_FMT,
			sdl2.SDL_TEXTUREACCESS_STREAMING,
			cfg.width, h
		)

		if not texture:
			raise Exception("Failed to create SDL2 Texture")
		
		textures.append(texture)

	if INDEX4LSB_WORKAROUND:
		blitted_queues.reverse()
		textures.reverse()

	prev_times = [time.time()]*60
	prev_time_i = 0
	running = True
	try:
		while running:
			e = sdl2.SDL_Event()
			while sdl2.SDL_PollEvent(e):
				if e.type == sdl2.SDL_QUIT:
					running = False
				if e.type == sdl2.SDL_KEYDOWN:
					if e.key.keysym.sym == sdl2.SDLK_ESCAPE:
						running = False
					if e.key.keysym.sym == sdl2.SDLK_q:
						running = False
			if not running:
				break
			
			y = 0
			for surface_queue, texture in zip(blitted_queues, textures):
				surface = surface_queue.get()
				sdl2.SDL_UpdateTexture(
					texture, None,
					surface.contents.pixels,
					surface.contents.pitch
				)
				h = surface.contents.h
				sdl2.SDL_FreeSurface(surface)
				sdl2.SDL_RenderCopy(
					renderer, texture, None,
					sdl2.SDL_Rect(0, y, cfg.width, h)
				)
				y += h

			sdl2.SDL_RenderPresent(renderer)

			now = time.time()
			fps = len(prev_times)/(now-prev_times[prev_time_i % len(prev_times)])
			msg = f"{round(fps)}fps ({round(fps*cfg.frameskip)}tps)" if prev_time_i > len(prev_times) else "??fps (??tps)"
			sdl2.SDL_SetWindowTitle(window, (f"pyswargol - {cfg.width}x{cfg.height} - " + msg).encode())
			prev_times[prev_time_i % len(prev_times)] = now
			prev_time_i += 1

	finally:
		for texture in textures:
			sdl2.SDL_DestroyTexture(texture)
		sdl2.SDL_DestroyRenderer(renderer)
		sdl2.SDL_DestroyWindow(window)
		sdl2.SDL_Quit()


def main(cfg: LifeConfig):
	# init sdl2 here so we can query screen size for fullscreen mode
	if sdl2.SDL_Init(sdl2.SDL_INIT_VIDEO) < 0:
		raise Exception("Failed to init SDL2")
	
	if cfg.fullscreen:
		dm = sdl2.SDL_DisplayMode()
		sdl2.SDL_GetDesktopDisplayMode(0, dm)
		print(f"Overriding fb size to match fullscreen resolution: {dm.w}x{dm.h}")
		cfg.width, cfg.height = dm.w, dm.h

	stopped = Event()

	# vertically split the framebuffer into close-to-equal sized chunks
	baseheight, rem = divmod(cfg.height, cfg.num_procs)
	section_heights = [baseheight] * (cfg.num_procs - rem) + [baseheight + 1] * rem
	assert(sum(section_heights) == cfg.height)

	blitted_queues = [Queue(1) for _ in range(cfg.num_procs)]

	wraparound_pipes = [Pipe() for _ in range(cfg.num_procs)]
	packed_result_pipes = [Pipe(duplex=False) for _ in range(cfg.num_procs)]
	life_procs = [
		Process(target=life_thread, args=[cfg, i, cfg.width, h, packed_result_pipes[i][1], wraparound_pipes[i][0], wraparound_pipes[(i+1)%cfg.num_procs][1]])
		for i, h in enumerate(section_heights)
	]
	for proc in life_procs:
		proc.start()

	blitter_threads = [
		Thread(target=blit_thread, args=[cfg, i, h, stopped, packed_result_pipes[i][0], blitted_queues[i]])
		for i, h in enumerate(section_heights)
	]
	for thread in blitter_threads:
		thread.start()

	try:
		gui_thread(cfg, section_heights, blitted_queues)
	except KeyboardInterrupt:
		print("Looks like you pressed Ctrl+C!")

	# The shutdown process is surprisingly fiddly to get right, without deadlocks
	print("Shutting down...")

	stopped.set() # tell the blitter threads to stop

	# unblock the blitters so they can "notice" the stop event
	for queue in blitted_queues:
		for surface in queue_purge(queue):
			sdl2.SDL_FreeSurface(surface)

	# wait for blitter threads to exit gracefully.
	for thread in blitter_threads:
		thread.join()

	print("Stopped blitters.")

	# final pass queue purge
	for queue in blitted_queues:
		for surface in queue_purge(queue):
			sdl2.SDL_FreeSurface(surface)

	# forcefully kill the life procs, trying to do this
	# cleanly without races is proving too difficult...
	for proc in life_procs:
		proc.kill()

	print("Stopped life procs.")

	# clean up the remaining Pipes
	# I think the gc will do this anyway but hey, nice to be explicit
	for a, b in wraparound_pipes:
		a.close()
		b.close()

	print("Bye!")


if __name__ == "__main__":
	from dataclass_argparser import parse_args_for_dataclass_or_exit

	cfg = parse_args_for_dataclass_or_exit(LifeConfig)

	print("Config:", cfg)
	main(cfg)

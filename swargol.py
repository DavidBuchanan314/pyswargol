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

Everything else happens within one process, so the Blitters can talk to the
main thread using standard Queues - the hard work here is done inside SDL2,
which does not hold the GIL.

"""

SURFACE_FMT = sdl2.SDL_PIXELFORMAT_ARGB8888


@dataclass
class LifeConfig:
	fb_width: int
	fb_height: int
	width_padding: int
	vsync: bool
	fullscreen: bool
	drylife: bool # whether to use the non-standard "drylife" algorithm.
	frameskip: int # only render 1-in-n frames to the screen
	num_procs: int # degree of parallelism

#cfg.fb_width, cfg.fb_height = (3840, 2160)
#cfg.fb_width, cfg.fb_height = (3024, 1890-2-64)
#cfg.fb_width, cfg.fb_height = (1920, 1080)
#cfg.fb_width, cfg.fb_height = (128, 64)
#cfg.width_padding = 16
#VSYNC = 1
#cfg.fullscreen = 0
#cfg.frameskip = 1 # 1 = no skipped frames, 2 = every other, 3 = every third, etc.
#cfg.num_procs = 8
#cfg.drylife = 1

# return all remaining items in a queue
def queue_purge(queue: Queue):
	try:
		while True:
			yield queue.get_nowait()
	except Empty:
		pass


def life_thread(cfg: LifeConfig, i, width, height, packed_pipe, pipe_top, pipe_bottom):
	WIDTH, HEIGHT = width, height

	STRIDE = WIDTH + cfg.width_padding
	STATE_BYTE_LENGTH = (STRIDE * HEIGHT) // 2
	COLSHIFT = STRIDE * 4
	WRAPSHIFT = STRIDE * HEIGHT * 4
	BIAS = (STRIDE + 2) * 4

	MASK_1 = int.from_bytes(b"\x11" * STATE_BYTE_LENGTH, "little") << BIAS
	MASK_CANVAS = int.from_bytes((b"\x11" * (WIDTH // 2) + b"\x00" * (cfg.width_padding // 2)) * HEIGHT, "little") << BIAS
	MASK_WRAP_LEFT = int.from_bytes((b"\x11" * ((cfg.width_padding // 2) // 2) + b"\x00" * ((WIDTH - cfg.width_padding // 2) // 2) + b"\x00" * (cfg.width_padding // 2)) * (HEIGHT + 2), "little") << (2 * 4)
	MASK_WRAP_RIGHT = int.from_bytes((b"\x00" * ((WIDTH - cfg.width_padding // 2) // 2) + b"\x11" * ((cfg.width_padding // 2) // 2) + b"\x00" * (cfg.width_padding // 2)) * (HEIGHT + 2), "little") << (2 * 4)
	MASK_NOT_3 = MASK_1 * (15 ^ 3)
	MASK_NOT_4 = MASK_1 * (15 ^ 4)
	MASK_NOT_7 = MASK_1 * (15 ^ 7)
	#WRAP_MASK = int.from_bytes(b"\x11" * (BIAS//8), "little") << BIAS # should that be BIAS//8???

	if 1:
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
			state |= ((state & MASK_WRAP_LEFT) << (WIDTH * 4)) | ((state & MASK_WRAP_RIGHT) >> (WIDTH * 4))

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

			packed_pipe.send_bytes(packed_state)

	except KeyboardInterrupt:
		print("life_thread SIGINT")
		while True:
			packed_pipe.send_bytes(bytes(STATE_BYTE_LENGTH)) # unblock any readers, until we get killed


def blit_thread(cfg: LifeConfig, i, stopped: Event, packed_queue, blitted_queue: Queue):
	while not stopped.is_set():
		packed_frame = packed_queue.recv_bytes() # this needs to stay in scope until SDL_ConvertSurfaceFormat is complete!
		surface = sdl2.SDL_CreateRGBSurfaceWithFormatFrom(
			packed_frame,
			cfg.fb_width, cfg.fb_height // cfg.num_procs, # XXX: use config
			4, # depth
			(cfg.fb_width + cfg.width_padding) // 2, # pitch
			sdl2.SDL_PIXELFORMAT_INDEX4LSB
		)
		sdl2.SDL_SetPaletteColors(surface.contents.format.contents.palette, sdl2.SDL_Color(40, 40, 40, 255), 0, 1)
		sdl2.SDL_SetPaletteColors(surface.contents.format.contents.palette, sdl2.SDL_Color(255, 255, 255, 255), 1, 1)
		blitted_queue.put(sdl2.SDL_ConvertSurfaceFormat(surface, SURFACE_FMT, 0))
		sdl2.SDL_FreeSurface(surface)
	
	print(f"blit_thread {i}: graceful exit")
	packed_queue.recv_bytes() # do a final read to un-block the writer
	packed_queue.close() # close our end of the Pipe


def gui_thread(cfg: LifeConfig, blitted_queues: List[Queue]):
	if sdl2.SDL_Init(sdl2.SDL_INIT_VIDEO) < 0:
		raise Exception("Failed to init SDL2")

	window = sdl2.SDL_CreateWindow(
		b"pysdl2 framebuffer test",
		sdl2.SDL_WINDOWPOS_UNDEFINED, sdl2.SDL_WINDOWPOS_UNDEFINED,
		cfg.fb_width, cfg.fb_height,
		sdl2.SDL_WINDOW_SHOWN
	)

	if not window:
		raise Exception("Failed to create SDL2 Window")

	if cfg.fullscreen:
		sdl2.SDL_SetWindowFullscreen(window, sdl2.SDL_WINDOW_FULLSCREEN)

	renderer = sdl2.SDL_CreateRenderer(window, -1, sdl2.SDL_RENDERER_ACCELERATED | (sdl2.SDL_RENDERER_PRESENTVSYNC if cfg.vsync else 0))

	if not renderer:
		raise Exception("Failed to create SDL2 Renderer")

	textures = []
	for _ in range(cfg.num_procs):
		texture = sdl2.SDL_CreateTexture(
			renderer,
			SURFACE_FMT,
			sdl2.SDL_TEXTUREACCESS_STREAMING,
			cfg.fb_width, cfg.fb_height // cfg.num_procs
		)

		if not texture:
			raise Exception("Failed to create SDL2 Texture")
		
		textures.append(texture)

	prev_times = [time.time()]*200
	prev_time_i = 0
	running = True
	#surface = blitted_queue.get()
	while running:
		e = sdl2.SDL_Event()
		while sdl2.SDL_PollEvent(e):
			if e.type == sdl2.SDL_QUIT:
				running = False
				break
			if e.type == sdl2.SDL_KEYDOWN:
				if e.key.keysym.sym == sdl2.SDLK_ESCAPE:
					running = False
					break
		
		for i, (surface_queue, texture) in enumerate(zip(blitted_queues, textures)):
			surface = surface_queue.get()
			sdl2.SDL_UpdateTexture(texture, None, surface.contents.pixels, surface.contents.pitch)
			sdl2.SDL_FreeSurface(surface)
			sdl2.SDL_RenderCopy(renderer, texture, None, sdl2.SDL_Rect(0, (cfg.fb_height//cfg.num_procs)*i, cfg.fb_width, cfg.fb_height//cfg.num_procs))

		sdl2.SDL_RenderPresent(renderer)

		now = time.time()
		fps = len(prev_times)/(now-prev_times[prev_time_i])
		msg = f"{fps:.1f}fps ({fps*cfg.frameskip:.1f}tps)"
		#print(msg)
		sdl2.SDL_SetWindowTitle(window, (f"pyswargol - {cfg.fb_width}x{cfg.fb_height} - " + msg).encode())
		prev_times[prev_time_i] = now
		prev_time_i = (prev_time_i + 1) % len(prev_times)

	sdl2.SDL_DestroyTexture(texture)
	sdl2.SDL_DestroyRenderer(renderer)
	sdl2.SDL_DestroyWindow(window)
	sdl2.SDL_Quit()


def main(cfg: LifeConfig):
	stopped = Event()

	assert((cfg.fb_height % cfg.num_procs) == 0)

	blitted_queues = [Queue(1) for _ in range(cfg.num_procs)]

	wraparound_pipes = [Pipe() for _ in range(cfg.num_procs)]
	packed_result_pipes = [Pipe(duplex=False) for _ in range(cfg.num_procs)]
	life_procs = [
		Process(target=life_thread, args=[cfg, i, cfg.fb_width, cfg.fb_height // cfg.num_procs, packed_result_pipes[i][1], wraparound_pipes[i][0], wraparound_pipes[(i+1)%cfg.num_procs][1]])
		for i in range(cfg.num_procs)
	]
	for proc in life_procs:
		proc.start()

	blitter_threads = [
		Thread(target=blit_thread, args=[cfg, i, stopped, packed_result_pipes[i][0], blitted_queues[i]])
		for i in range(cfg.num_procs)
	]
	for thread in blitter_threads:
		thread.start()

	try:
		gui_thread(cfg, blitted_queues)
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
	cfg = LifeConfig(
		fb_width=1920,
		fb_height=1080,
		width_padding=16,
		vsync=True,
		fullscreen=False,
		drylife=True,
		frameskip=1,
		num_procs=8
	)
	main(cfg)

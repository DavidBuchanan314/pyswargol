import clize
import sys

def parse_args_for_dataclass_or_exit(dataclass, argv=sys.argv):
	cli = clize.Clize.get_cli(dataclass)
	try:
		args = cli(*argv)
	except clize.errors.ArgumentError as e:
		print(e, file=sys.stderr)
		sys.exit(-1)
	
	# help text
	if type(args) is not dataclass:
		print(args)
		sys.exit(0)
	
	return args
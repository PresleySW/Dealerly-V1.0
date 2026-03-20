import asyncio
import nest_asyncio

try:
    loop = asyncio.get_event_loop()
except RuntimeError:
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

nest_asyncio.apply(loop)

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "Dealerly 0.9"))

from dealerly.cli import main
main()





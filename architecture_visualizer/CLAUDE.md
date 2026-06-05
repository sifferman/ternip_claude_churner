
I want to visuaully represent the architecture of Ternip using a graph layout library.

I imagine that each design should have the following nodes:

DRAM bank0 (fixed location)
DRAM bank1 (fixed location)
DRAM bank2 (fixed location)
DRAM bank3 (fixed location)

tmatmul_dma[bank0]
tmatmul_dma[bank1]
tmatmul_dma[bank2]
tmatmul_dma[bank3]

RMS+loadstore+rowwise_op+vector_registers all as 1 node
instruction decode/fifos as 1 node

multioperand_accumulator[bank0]
multioperand_accumulator[bank1]
multioperand_accumulator[bank2]
multioperand_accumulator[bank3]
tmatmul_importvector[bank0]
tmatmul_importvector[bank1]
tmatmul_importvector[bank2]
tmatmul_importvector[bank3]
tmatmul state machine


And depending on the architecture, (NumSeparateAxiInstances/NumDdrBanksPerTmatmul/NumDdrBanksPerTmatmul), the nodes will be connected slightly differently.

The graph should be build with the following:

each node should be represented by how many cells it has
each node is connected to every other node depending on how many wires connect them (bus size)

The gui should have sliders for

TmatmulParallelism
VectorParallelism
BatchSize
NumDdrBanksUsed (will affect how many nodes there are)

which should affect the size of the nodes, and the size of the edges.
Edges should visually get larger as the number of wires in that bus widens

The graph visualizer should be lightweight, and nodes should be draggable, and the sliders should automatically update the number of nodes, the size of nodes, and the size of edges

we should also have a statistic shown with total wire length, which is (FOR ALL EDGES, SUM buswidth * edgelength)

also, we should give an estimated tokens/second based off ternary_matmul/sw_utils/target/report_instruction_timing.py

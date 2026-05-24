# synth_json.tcl
#
# Yosys TCL script that synthesizes the design with the xc7 cell library
# and writes a JSON netlist for offline fanout analysis.
#
# Usage:
#   yosys -p 'tcl scripts/synth_json.tcl <rtl.sv2v.v> <output.json>'
#
# The xc7 cell library is used because it's fast (synth_xilinx -family xc7
# completes in ~1-3 min on this design vs Vivado's 30+ min). The LOGICAL
# fanout reported by this flow is independent of device choice -- we're
# counting net sinks, not estimating routing delay.

set rtl [lindex $argv 0]
set out_json [lindex $argv 1]

yosys -import

read_verilog $rtl

synth_xilinx -top ternip_core -family xc7

# Optimize ones more pass to give us a representative final netlist (and
# clean up any dangling wires that would distort fanout counts).
opt -full

write_json $out_json

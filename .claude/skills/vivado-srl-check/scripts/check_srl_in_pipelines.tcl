# check_srl_in_pipelines.tcl
#
# Verify that NONE of the pipelined-interconnect wrapper modules
# (ternip_pipelined_interconnect, axi_ternip_pipelined_interconnect_rd,
# axi_ternip_pipelined_interconnect_wr) had their FFs collapsed into
# SRL16E / SRL32E / SRLC32E primitives during synthesis.
#
# SRL primitives live entirely within one SLICEM and CANNOT be split
# across SLR boundaries. These wrappers exist specifically to give
# Vivado a chain of FFs that the placer can distribute across LAGUNA
# register tiles for cross-SLR routing. If any one stage collapses to
# an SRL, the LAGUNA distribution for that signal is impossible and
# the SLR-crossing recovery this design depends on falls apart.
#
# Usage:
#   vivado -mode batch -nojournal -nolog \
#       -source check_srl_in_pipelines.tcl \
#       -tclargs <path/to/project.xpr | path/to/routed.dcp>
#
# Returns exit code 0 if no SRLs are inferred under any pipelined-
# interconnect wrapper scope; exit code 1 if any SRL is found (with
# the full list printed). Greppable output:
#   SRL_CHECK_TOTAL=<n>
#   SRL_CHECK_OK=true|false
#
# Pattern matches what pre_synth_design.tcl Section 3 protects.

if {[llength $argv] < 1} {
    puts "ERROR: usage: vivado -mode batch -source check_srl_in_pipelines.tcl -tclargs <xpr|dcp>"
    exit 1
}

set artifact [lindex $argv 0]
if {![file exists $artifact]} {
    puts "ERROR: artifact not found: $artifact"
    exit 1
}

puts "Opening: $artifact"
switch -glob -- $artifact {
    "*.xpr" {
        open_project $artifact
        set run [lindex [get_runs -filter {IS_IMPLEMENTATION && PROGRESS == "100%"} -quiet] 0]
        if {$run eq ""} {
            puts "ERROR: no completed impl run in project"
            exit 1
        }
        open_run $run
    }
    "*.dcp" {
        open_checkpoint $artifact
    }
    default {
        puts "ERROR: unrecognized artifact (need .xpr or .dcp): $artifact"
        exit 1
    }
}

# Scopes to protect (same set as pre_synth_design.tcl Section 3).
set scope_patterns {
    "*buffer_m_axi_*"
    "*buffer_tmatmul_desc*"
    "*buffer_loadstore_*"
    "*buffer_instruction*"
}

set total 0
set found_any 0
foreach pat $scope_patterns {
    set srls [get_cells -quiet -hier -filter "REF_NAME =~ SRL* && NAME =~ $pat"]
    set n [llength $srls]
    if {$n > 0} {
        set found_any 1
        incr total $n
        puts "SRL_CHECK_VIOLATION: $n SRL primitive(s) inside scope $pat:"
        foreach c [lrange $srls 0 9] {
            puts "  $c  ([get_property REF_NAME $c])"
        }
        if {$n > 10} { puts "  ... (and [expr {$n - 10}] more)" }
    } else {
        puts "SRL_CHECK_OK: 0 SRL primitives in scope $pat"
    }
}

# Also check the chained-AXI4-register-slice stage paths.
set stage_srls [get_cells -quiet -hier -filter {REF_NAME =~ SRL* && NAME =~ *.stages\[*\].inst*}]
if {[llength $stage_srls] > 0} {
    set found_any 1
    incr total [llength $stage_srls]
    puts "SRL_CHECK_VIOLATION: [llength $stage_srls] SRL primitive(s) in chained-AXI4 stages:"
    foreach c [lrange $stage_srls 0 9] {
        puts "  $c  ([get_property REF_NAME $c])"
    }
} else {
    puts "SRL_CHECK_OK: 0 SRL primitives in chained-AXI4 stages"
}

puts "SRL_CHECK_TOTAL=$total"
if {$found_any} {
    puts "SRL_CHECK_OK=false"
    exit 1
} else {
    puts "SRL_CHECK_OK=true"
    exit 0
}

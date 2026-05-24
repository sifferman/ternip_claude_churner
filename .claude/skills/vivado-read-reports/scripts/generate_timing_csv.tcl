# generate_timing_csv.tcl
#
# Vivado batch-mode script that opens a project, opens the most recent
# routed implementation run, and writes a CSV of every failing setup path.
#
# Usage:
#   vivado -mode batch -nojournal -nolog \
#       -source generate_timing_csv.tcl \
#       -tclargs <path/to/project.xpr> <path/to/output.csv>
#
# The script first calls report_timing_summary to learn the number of
# failing endpoints (so the second report is sized exactly to the failures).
# WNS and TNS are printed to stdout in the form:
#   VIVADO_READ_REPORTS_WNS=<value>
#   VIVADO_READ_REPORTS_TNS=<value>
#   VIVADO_READ_REPORTS_NFAIL=<value>
#   VIVADO_READ_REPORTS_CSV=<path>
# A shell wrapper greps for those tags.

if {[llength $argv] < 2} {
    puts "ERROR: usage: vivado -mode batch -source generate_timing_csv.tcl -tclargs <xpr> <csv> \[cells_filter\]"
    exit 1
}

set xpr_path [lindex $argv 0]
set csv_path [lindex $argv 1]
# Optional 3rd arg: cell hierarchy pattern (e.g. level0_i/level1/level1_i/ulp/ternip_ip_1).
# When set, restricts the timing report to setup paths whose start AND end
# points are inside that cell hierarchy. Mirrors the GUI Timing Summary
# panel's "Cell Filter" / report_timing -cells behavior. Empty = full design.
set cells_filter ""
if {[llength $argv] >= 3} {
    set cells_filter [lindex $argv 2]
}

if {![file exists $xpr_path]} {
    puts "ERROR: project file not found: $xpr_path"
    exit 1
}

puts "Opening project: $xpr_path"
open_project $xpr_path

# Pick the most recent routed run. Vitis projects use impl_1; if multiple
# impl runs exist, pick the most recently finished one.
set candidate_runs [get_runs -filter {IS_IMPLEMENTATION} -quiet]
if {[llength $candidate_runs] == 0} {
    puts "ERROR: no implementation runs in project"
    exit 1
}

# Prefer impl_1 if it exists and is routed; otherwise pick the most
# recently completed one.
set chosen_run ""
foreach run $candidate_runs {
    set status [get_property STATUS $run]
    set progress [get_property PROGRESS $run]
    if {[string match "*route_design Complete*" $status] ||
        [string match "*phys_opt_design Complete*" $status] ||
        [string match "*Out-of-Context*" $status] ||
        ($progress eq "100%")} {
        set chosen_run $run
        if {[get_property NAME $run] eq "impl_1"} {
            break
        }
    }
}
if {$chosen_run eq ""} {
    # Fall back to whatever exists -- open_run may still succeed if route
    # was partial.
    set chosen_run [lindex $candidate_runs 0]
}

set run_name [get_property NAME $chosen_run]
puts "Opening run: $run_name (status: [get_property STATUS $chosen_run])"
open_run $run_name

# First pass: get WNS, TNS, and number of failing endpoints from the summary.
report_timing_summary -no_header -no_detailed_paths -file /tmp/_vrr_timing_summary.rpt

# Pull headline values directly from the design properties (more reliable
# than parsing the text report).
set wns [get_property SLACK [get_timing_paths -setup -max_paths 1 -nworst 1]]
set tns [get_property STATS.WNS [current_design]]
# STATS.WNS is sometimes set; the canonical scalar TNS lives under
# report_design_analysis output, but we get it directly by summing the
# worst-slack per endpoint -- below.

# Count failing setup endpoints by querying paths with slack < 0. We pass
# a very large max_paths because we want all of them.
#
# Note: -nworst N controls how many paths per endpoint to return; we want 1
# per endpoint (the worst) which is what the GUI's timing summary shows.
set failing_paths [get_timing_paths -setup -slack_lesser_than 0 \
                      -max_paths 1000000 -nworst 1]

# If cells_filter is set, restrict to paths whose start AND end points are
# inside that cell hierarchy. This mirrors the GUI's "Cells:" field on the
# Timing Summary panel / report_timing -cells. We filter by string-prefix
# match on STARTPOINT_PIN and ENDPOINT_PIN because get_timing_paths in
# Vivado 2023.1 does NOT accept a -cells flag (only report_timing does).
#
# Without this filter, unfiltered Vitis builds also pick up paths through
# platform infrastructure (DPA profile monitors, debug instrumentation,
# AXI interconnect) that don't gate bitstream generation and dilute the
# relevant cluster analysis.
if {$cells_filter ne ""} {
    set scope_prefix "${cells_filter}/"
    set filtered {}
    foreach p $failing_paths {
        set from_pin [get_property STARTPOINT_PIN $p]
        set to_pin   [get_property ENDPOINT_PIN $p]
        if {[string match "${scope_prefix}*" $from_pin] &&
            [string match "${scope_prefix}*" $to_pin]} {
            lappend filtered $p
        }
    }
    set total_before [llength $failing_paths]
    set failing_paths $filtered
    set total_after [llength $failing_paths]
    puts "Filtered by cells '$cells_filter': $total_after of $total_before paths kept"
}
set nfail [llength $failing_paths]

# Compute TNS as the sum of failing slacks.
set tns 0.0
foreach p $failing_paths {
    set tns [expr {$tns + [get_property SLACK $p]}]
}

# WNS = worst (most negative) slack among the failing paths, or 0 if none.
if {$nfail > 0} {
    set wns [get_property SLACK [lindex $failing_paths 0]]
} else {
    set wns 0.0
}

puts "VIVADO_READ_REPORTS_WNS=$wns"
puts "VIVADO_READ_REPORTS_TNS=$tns"
puts "VIVADO_READ_REPORTS_NFAIL=$nfail"

# Extract all per-path properties into rows up front, then sort rows by
# the From column (STARTPOINT_PIN) so related nets cluster together --
# matches how the user inspects the exported XLSX manually.
#
# Property names on timing_path objects vary slightly between Vivado
# versions (e.g. CLOCK_UNCERTAINTY does not exist in 2023.1 even though
# the GUI's Timing Summary panel shows that column). get_prop_safe tries
# the requested property and returns "" if Vivado errors out -- matches
# the behavior of the Export to Spreadsheet output, which leaves the
# cell blank when the value isn't available.
proc get_prop_safe {prop_name obj} {
    if {[catch {get_property $prop_name $obj} val]} {
        return ""
    }
    return $val
}

set rows {}
set i 0
foreach p $failing_paths {
    incr i
    set from   [get_prop_safe STARTPOINT_PIN $p]
    set to     [get_prop_safe ENDPOINT_PIN $p]
    set slack  [get_prop_safe SLACK $p]
    set levels [get_prop_safe LOGIC_LEVELS $p]
    set fanout [get_prop_safe MAX_FANOUT $p]
    set total  [get_prop_safe DATAPATH_DELAY $p]
    set logic  [get_prop_safe DATAPATH_LOGIC_DELAY $p]
    set net    [get_prop_safe DATAPATH_NET_DELAY $p]
    set req    [get_prop_safe REQUIREMENT $p]
    set sclk   [get_prop_safe STARTPOINT_CLOCK $p]
    set dclk   [get_prop_safe ENDPOINT_CLOCK $p]
    set excpt  [get_prop_safe EXCEPTION $p]
    set uncert [get_prop_safe CLOCK_UNCERTAINTY $p]
    lappend rows [list \
        $from "Path $i" $slack $levels $fanout $to \
        $total $logic $net $req $sclk $dclk $excpt $uncert]
}

# Sort rows by the From field (element 0). -dictionary handles array
# index brackets sensibly (e.g. reg[2] sorts before reg[10]).
set rows [lsort -dictionary -index 0 $rows]

# Write CSV.
set fd [open $csv_path w]
puts $fd "From,Name,Slack,Levels,High Fanout,To,Total Delay,Logic Delay,Net Delay,Requirement,Source Clock,Destination Clock,Exception,Clock Uncertainty"
foreach row $rows {
    # Wrap fields that may contain commas in quotes -- pin paths can have
    # commas in array indices (rare) or other punctuation. Cell paths in
    # this project don't, but quote to be safe.
    set out_fields {}
    foreach f $row {
        if {[regexp {[,"\n]} $f]} {
            set escaped [string map {\" \"\"} $f]
            lappend out_fields "\"$escaped\""
        } else {
            lappend out_fields $f
        }
    }
    puts $fd [join $out_fields ","]
}
close $fd

puts "VIVADO_READ_REPORTS_CSV=$csv_path"
puts "Wrote $nfail failing paths to $csv_path"

close_project
exit 0

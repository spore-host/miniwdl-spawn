version 1.0

# Composition-test workflow (NOT a usage example — see examples/hello.wdl for that).
#
# Deliberately has NO File outputs. The composition test runs this against the
# Substrate AWS emulator, which does not execute the task's workload — so no
# output files are ever produced on the (emulated) instance. This task therefore
# exercises exactly the seam the test can honestly prove: miniwdl drives the spawn
# backend → the backend dispatches `spawn task run`, polls the (Substrate-seeded)
# completion record, and returns its exit code → miniwdl marks the task
# succeeded/failed by that code. Output collection (which needs a real workload)
# is out of scope for a no-execution emulator and is covered by the real-AWS run.

workflow noout {
  call step {}
}

task step {
  command <<<
    echo "composition step ran"
  >>>
  runtime {
    docker: "ubuntu:24.04"
    cpu: 2
    memory: "4 GB"
  }
}

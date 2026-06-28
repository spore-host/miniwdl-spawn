version 1.0

# Minimal smoke test for the spawn backend: one task, sized from runtime{},
# run in a container on an ephemeral EC2 instance.
#   miniwdl run examples/hello.wdl name=world --cfg scheduler.container_backend=spawn

workflow hello {
  input { String name = "world" }
  call greet { input: name = name }
  output { File greeting = greet.out }
}

task greet {
  input { String name }
  command <<<
    echo "hello, ~{name}, from $(hostname) on $(uname -m)" > greeting.txt
  >>>
  output { File out = "greeting.txt" }
  runtime {
    docker: "ubuntu:24.04"
    cpu: 2
    memory: "4 GB"
  }
}

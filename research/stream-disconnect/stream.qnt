// Private aiming model: FastAPI SSE endpoint teardown when the client disconnects.
// As read from fastapi/routing.py (0.162.5): a producer task iterates the endpoint
// generator into a buffer, a keepalive task forwards the buffer to the response,
// and the request exit stack unwinds: close the output stream, cancel both tasks,
// then exit the yield dependencies. FIXED switches to a proposed design.
module stream {
  type Where = InGenerator | BlockedSending | Finished
  type Step = Streaming | CloseOutput | CancelTasks | ExitDeps | Collected

  const FIXED: bool

  var step: Step
  var producer: Where        // where the producer task is suspended
  var keepalive: Where       // InGenerator here means "waiting for the producer"
  var genClosed: bool
  var depsOpen: bool
  var genClosedWhileDepsOpen: bool
  var errorSeenByDeps: bool
  var pendingError: bool

  action init = all {
    step' = Streaming, producer' = InGenerator, keepalive' = InGenerator,
    genClosed' = false, depsOpen' = true, genClosedWhileDepsOpen' = false,
    errorSeenByDeps' = false, pendingError' = false,
  }

  action stay = all {
    genClosed' = genClosed, depsOpen' = depsOpen, pendingError' = pendingError,
    genClosedWhileDepsOpen' = genClosedWhileDepsOpen, errorSeenByDeps' = errorSeenByDeps,
  }

  action step_ = any {
    // while streaming, each task is either waiting or blocked on a full buffer
    all { step == Streaming, stay, step' = Streaming,
          nondet p = Set(InGenerator, BlockedSending).oneOf()
          nondet k = Set(InGenerator, BlockedSending).oneOf()
          all { producer' = p, keepalive' = k } },
    // the client disconnects: the exit stack starts to unwind
    all { step == Streaming, stay, producer' = producer, keepalive' = keepalive,
          step' = if (FIXED) CancelTasks else CloseOutput },
    // 1. the output stream is closed first; a keepalive task blocked sending fails
    all { step == CloseOutput, step' = CancelTasks, producer' = producer,
          keepalive' = if (keepalive == BlockedSending) Finished else keepalive,
          pendingError' = keepalive == BlockedSending,
          genClosed' = genClosed, depsOpen' = depsOpen,
          genClosedWhileDepsOpen' = genClosedWhileDepsOpen, errorSeenByDeps' = errorSeenByDeps },
    // 2. both tasks are cancelled. Cancelling inside the generator runs its cleanup.
    //    Cancelling a producer blocked sending leaves the generator suspended,
    //    unless the design closes it explicitly.
    all { step == CancelTasks, step' = ExitDeps, producer' = Finished, keepalive' = Finished,
          val closes = producer == InGenerator or FIXED
          all { genClosed' = closes, genClosedWhileDepsOpen' = closes },
          depsOpen' = depsOpen, pendingError' = pendingError, errorSeenByDeps' = errorSeenByDeps },
    // 3. yield dependencies exit, and see any error raised by the steps above
    all { step == ExitDeps, step' = Collected, depsOpen' = false, errorSeenByDeps' = pendingError,
          producer' = producer, keepalive' = keepalive, genClosed' = genClosed,
          pendingError' = pendingError, genClosedWhileDepsOpen' = genClosedWhileDepsOpen },
    // 4. later, the garbage collector finalizes a generator nobody closed
    all { step == Collected, step' = Collected, genClosed' = true, depsOpen' = depsOpen,
          producer' = producer, keepalive' = keepalive, pendingError' = pendingError,
          genClosedWhileDepsOpen' = genClosedWhileDepsOpen, errorSeenByDeps' = errorSeenByDeps },
  }

  val cleanupBeforeDeps = (genClosed and step == Collected) implies genClosedWhileDepsOpen
  val depsExitCleanly = not(errorSeenByDeps)
}

module current { import stream(FIXED = false).* }
module design { import stream(FIXED = true).* }

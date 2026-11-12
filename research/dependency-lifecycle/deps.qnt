// Private aiming model: FastAPI yield-dependency lifecycle for one request.
// Three dependencies in a chain  top -> mid -> leaf  (top is what the endpoint asks for).
// Each is a generator (has teardown) or plain, and each edge carries the declared scope.
module deps {
  type Scope = Function | Request | Unset
  type Phase = Setup | Endpoint | CloseFunction | Respond | CloseRequest | Done

  // configuration, chosen nondeterministically in init
  var gen: str -> bool            // is the dependency a generator
  var edge: str -> Scope          // scope declared on the Depends() that points at it
  // run state
  var phase: Phase
  var open: Set[str]              // generators entered and not yet exited
  var closedEarly: Set[str]       // (dep, sub) pairs detected: dep exited after sub
  var cancelled: bool
  var exits: str -> int

  pure val NODES = Set("top", "mid", "leaf")
  pure val SUBS = Map("top" -> Set("mid", "leaf"), "mid" -> Set("leaf"), "leaf" -> Set())
  pure val PARENT = Map("mid" -> "top", "leaf" -> "mid")

  // scope the code computes for a dependant: declared, else "request" for generators
  def computed(n: str): Scope =
    if (edge.get(n) != Unset) edge.get(n) else if (gen.get(n)) Request else Unset

  // which exit stack a generator lands on (solve_dependencies)
  def stack(n: str): Scope = if (edge.get(n) == Function) Function else Request

  // the startup check as written: direct parent only (get_dependant)
  val accepted = Set("mid", "leaf").forall(n =>
    not(computed(PARENT.get(n)) == Request and edge.get(n) == Function))

  action init = {
    nondet g = NODES.setOfMaps(Set(true, false)).oneOf()
    nondet e = NODES.setOfMaps(Set(Function, Request, Unset)).oneOf()
    all {
      gen' = g, edge' = e, phase' = Setup, open' = Set(), closedEarly' = Set(),
      cancelled' = false, exits' = NODES.mapBy(_ => 0),
    }
  }

  action close(s: Scope, nxt: Phase): bool = {
    val closing = open.filter(n => stack(n) == s)
    all {
      open' = open.exclude(closing),
      exits' = exits.keys().mapBy(n => exits.get(n) + (if (closing.contains(n)) 1 else 0)),
      // a generator still open that (transitively) uses one closing now
      closedEarly' = closedEarly.union(
        open.exclude(closing).filter(n => SUBS.get(n).intersect(closing).size() > 0)),
      phase' = nxt, gen' = gen, edge' = edge, cancelled' = cancelled,
    }
  }

  action keep(nxt: Phase, c: bool): bool = all {
    phase' = nxt, cancelled' = c, open' = open, closedEarly' = closedEarly,
    exits' = exits, gen' = gen, edge' = edge,
  }

  action step = any {
    all { phase == Setup, accepted, phase' = Endpoint, open' = NODES.filter(n => gen.get(n)),
          closedEarly' = closedEarly, exits' = exits, gen' = gen, edge' = edge,
          cancelled' = cancelled },
    all { phase == Endpoint, keep(CloseFunction, false) },
    // client disconnect / task cancellation: unwind without responding
    all { phase == Endpoint, keep(CloseFunction, true) },
    all { phase == CloseFunction, close(Function, if (cancelled) CloseRequest else Respond) },
    all { phase == Respond, any { keep(CloseRequest, false), keep(CloseRequest, true) } },
    all { phase == CloseRequest, close(Request, Done) },
  }

  // Docs: "any dependency needs to be able to run its exit code before the sub-dependencies"
  val exitBeforeSubs = closedEarly.size() == 0
  val exactlyOnce = phase == Done implies
    NODES.forall(n => exits.get(n) == (if (gen.get(n)) 1 else 0))
}

# Algebraic model

This document defines a small, composable model for an agentic harness. It describes the stable work performed by the core, the behavior supplied by plugins, the state of one run, and the conditions under which that run continues, pauses, completes, fails, or is cancelled.

The model does not prescribe a programming language, model provider, storage system, transport, or user interface. A concrete implementation must choose those things while preserving the relationships defined here.

## 1. How to read the notation

The model begins with values and history, then introduces plugins, model requests, work requests, and finally the complete loop. Every symbol is introduced before it is used in a later definition.

| Notation | Plain-language meaning |
| --- | --- |
| $x\in\mathcal X$ | $x$ is a member of the set $\mathcal X$. |
| $\mathbb N$ | The natural numbers, including zero: $\{0,1,2,\ldots\}$. |
| $\mathcal X\times\mathcal Y$ | All ordered pairs with one member from $\mathcal X$ and one from $\mathcal Y$. |
| $\mathcal X\sqcup\mathcal Y$ | A union that keeps members of $\mathcal X$ distinguishable from members of $\mathcal Y$. |
| $\mathcal X^{*}$ | All finite ordered sequences whose entries belong to $\mathcal X$. |
| $[]$ | The empty sequence. |
| $[x_1,\ldots,x_n]$ | An ordered sequence of $n$ entries. Entries may repeat. |
| $A\mathbin{\Vert}B$ | The sequence obtained by appending $B$ after $A$. |
| $|A|$ | The number of entries in a finite set or sequence $A$. |
| $\varnothing$ | No value is present. |
| $\subseteq$ | Every member of the set on the left also belongs to the set on the right. |
| $\rightarrow$ | A function maps every allowed input on the left to one output on the right. |
| $\rightharpoonup$ | A function may be undefined for some inputs or may never return. |
| $g\circ f$ | Apply $f$ first and then apply $g$. |
| $=$ | The two sides are equal. |
| $\ne$ | The two sides are not equal. |
| $\land$ | Both surrounding conditions hold. |
| $\Longrightarrow$ | The condition on the left implies the condition on the right. |
| $\prec$ | The plugin on the left must run before the plugin on the right. |

Calligraphic letters such as $\mathcal X$ denote sets. Ordinary lowercase letters such as $x$ denote individual values. Ordered sequences use square brackets, while tuples with fixed fields use parentheses.

Subscripts identify a particular run step, plugin, or item. For example, $L_k$ is the ledger at model-call number $k$, while $z_i$ is the private state of plugin $i$. A subscript is only a label unless the surrounding definition says otherwise.

## 2. The boundary of the model

An agentic harness conducts one application-level turn by repeatedly doing the following:

1. prepare a request from the current run history;
2. ask a model to produce a response;
3. record that response;
4. if the response requests work, obtain and record the results;
5. ask the model again with the enlarged history;
6. return only when the response contains a valid final value and requests no more work.

The core owns this repetition. It does not own the features placed around the steps.

In particular, the core has no built-in concept of:

- a system prompt;
- reminders;
- context compaction;
- available tools;
- tool execution;
- a particular model provider;
- approvals;
- handoffs;
- retry or turn limits.

Plugins may provide those features through the stable boundaries introduced below. The core depends only on those boundaries and never on a particular plugin.

## 3. Identities, bodies, and types

Let

$$
\mathscr I
$$

be the set of possible item identities. Every recorded item receives one identity $\iota\in\mathscr I$. Identities are unique within one run and are not reused during that run.

Let

$$
\mathscr B
$$

be the set of possible item bodies. The core stores a body but does not need to understand its feature-specific contents.

Let

$$
\mathscr T
$$

be the set of specific item types defined by public plugin protocols. A specific type may identify a model message, function call, approval request, reminder, or any later extension. The core does not contain a fixed list of these types.

The core does need a smaller structural classification:

$$
\mathcal K=\{\mathbf V,\mathbf Q,\mathbf R\}.
$$

The three members have the following meanings.

- $\mathbf V$ identifies a retained value. It records information but does not itself require the harness to perform work.
- $\mathbf Q$ identifies a request for work.
- $\mathbf R$ identifies a result that resolves an earlier request.

A tool call is therefore not a core type. It is a plugin-defined specific type whose structural class is $\mathbf Q$. A tool result has structural class $\mathbf R$ and refers to the request it resolves.

## 4. History items

Let $\mathcal X$ be the set of all history items. An item $x\in\mathcal X$ has the form

$$
x=
\left(
\iota_x,
\kappa_x,
\tau_x,
\rho_x,
b_x
\right).
$$

Its fields are as follows.

- $\iota_x\in\mathscr I$ is the stable identity of $x$.
- $\kappa_x\in\mathcal K$ is its core structural type.
- $\tau_x\in\mathscr T$ is its specific plugin-defined type.
- $\rho_x\in\mathscr I\sqcup\{\varnothing\}$ identifies an earlier related item, or is $\varnothing$ when there is no such reference.
- $b_x\in\mathscr B$ is its body.

Every result must identify the request it resolves:

$$
\kappa_x=\mathbf R
\Longrightarrow
\rho_x\ne\varnothing.
$$

The referenced item must already occur in the same run and must be a request:

$$
\kappa_x=\mathbf R
\land
\rho_x=\iota_q
\Longrightarrow
\kappa_q=\mathbf Q.
$$

Here $q$ denotes the earlier item whose identity is $\iota_q$.

Let

$$
\mathcal Q
\subseteq
\mathcal X
$$

be the set of items whose structural type is $\mathbf Q$, and let

$$
\mathcal R
\subseteq
\mathcal X
$$

be the set of items whose structural type is $\mathbf R$.

## 5. The authoritative ledger

Let

$$
L=[x_0,x_1,\ldots,x_n]\in\mathcal X^{*}
$$

be the ledger of a run. It is the complete ordered sequence of items that the harness has accepted as part of that run.

The ledger is append-only. A transition may replace $L$ only with

$$
L'=L\mathbin{\Vert}X
$$

for some $X\in\mathcal X^{*}$. Existing ledger items retain their identities, order, structural types, specific types, references, and bodies.

This rule separates historical truth from the view prepared for a model. Compaction may shorten the model-facing view, but it does not erase the source items from the ledger. A plugin that needs durable summarized history may append a new summary item and retain references to its sources through its own specific protocol.

## 6. Plugins and their private state

Let

$$
\mathcal P=\{p_1,p_2,\ldots,p_m\}
$$

be the finite set of plugins selected for an agent definition. The number $m$ is the number of selected plugins.

Each plugin $p_i$ owns private state

$$
z_i\in\mathcal Z_i.
$$

The set $\mathcal Z_i$ contains all valid states of plugin $p_i$. The core may store and return $z_i$, but only $p_i$ interprets it.

The combined plugin state is

$$
Z=
\left(
z_1,z_2,\ldots,z_m
\right).
$$

A plugin may provide behavior at one or more public boundaries. It need not provide behavior at every boundary. For example, a reminder plugin may only change a model request, while a tool plugin may both advertise a capability and handle matching requests.

The application that assembles an agent knows the core and the selected plugins. A plugin knows the public core contracts that it implements. The core does not import, name, or branch on any selected plugin.

## 7. Plugin order

Plugin discovery and loading order have no meaning in the model. The same agent definition must not change because a filesystem, package manager, or runtime discovers plugins in another order.

Some plugin operations are nevertheless order-sensitive. Let

$$
p_i\prec p_j
$$

mean that $p_i$ must run before $p_j$ when both operate at the same boundary.

The precedence relation must contain no cycle. For example, an agent definition cannot contain both

$$
p_i\prec p_j
$$

and

$$
p_j\prec p_i.
$$

Before a run begins, the assembler resolves each boundary into an explicit ordered sequence

$$
\Lambda=[p_{i_1},p_{i_2},\ldots,p_{i_r}].
$$

Here $r$ is the number of plugins active at that boundary. The resolved sequence must respect every declared precedence relation. It becomes part of the agent definition and therefore remains fixed for the run.

Independent contributions should use a combination rule whose result does not depend on order. If two plugins claim the same unique capability name or make incompatible contributions, assembly fails. The system does not silently let the last-loaded plugin win.

## 8. The immutable agent definition

Let

$$
\Gamma
$$

denote the complete agent definition chosen for one run. It contains:

- the selected plugins $\mathcal P$;
- their initial states;
- their public protocol versions;
- their resolved ordering $\Lambda$ at each boundary;
- the mapping from specific request types to handlers;
- the rules for validating a final value;
- any installed policies, such as retries, approvals, or turn limits;
- the abstract external capabilities required by the selected plugins.

For the compact notation used later, write

$$
\Gamma=
\left(
\mathcal P,
\Lambda,
\Pi,
\Theta,
Z_0
\right).
$$

Here $\Pi$ collects the public data rules and handler registrations shared by the selected plugins. $\Theta$ collects their installed validation, recovery, approval, retry, and limit policies together with their required external capabilities. The preceding list gives the full meaning of the tuple; $\Pi$ and $\Theta$ merely keep later equations readable.

$\Gamma$ is immutable during a run. Plugin state may change, but the identity, order, contracts, and authority of the selected plugins do not change silently. A feature that intentionally changes the active composition must do so through an explicit protocol whose effects are recorded.

Let $\mathcal U$ be the set of paused states, $\mathcal E$ the set of failure records, and $\mathcal C_A$ the set of cancellation records. The subscript $A$ distinguishes $\mathcal C_A$ from the model-request set $\mathcal C$ introduced next.

For any set $\mathcal A$, define

$$
\mathfrak O(\mathcal A)
=
\mathcal A
\sqcup
\mathcal U
\sqcup
\mathcal E
\sqcup
\mathcal C_A.
$$

$\mathfrak O(\mathcal A)$ is the set of possible outcomes from a stage whose ordinary successful value belongs to $\mathcal A$. The stage may instead pause, fail, or be cancelled. Equations below that unpack an ordinary value apply only when the stage returned that successful branch.

## 9. Model-facing requests

Let

$$
\mathcal C
$$

be the set of provider-independent model requests understood by the public model boundary. A value $c\in\mathcal C$ may contain an ordered context, capability descriptions, output requirements, and model settings. The core does not invent any of those contributions.

The selected preparation plugins collectively define

$$
P_\Gamma:
\mathcal X^{*}\times\mathcal Z_1\times\cdots\times\mathcal Z_m
\rightarrow
\mathfrak O
\left(
\mathcal C\times\mathcal Z_1\times\cdots\times\mathcal Z_m
\right).
$$

Given the current ledger and plugin states, $P_\Gamma$ returns a prepared model request and updated plugin states.

At model-call number $k$:

$$
\left(
c_k,
Z_k^{P}
\right)
=
P_\Gamma
\left(
L_k,
Z_k
\right).
$$

Here:

- $L_k$ is the ledger before model call $k$;
- $Z_k$ is the combined plugin state before preparation;
- $c_k$ is the prepared request;
- $Z_k^{P}$ is the plugin state after preparation;
- the superscript $P$ labels this intermediate state as the result of preparation.

A system-prompt plugin, reminder plugin, tool-advertisement plugin, and compaction plugin are all possible contributors to $P_\Gamma$. None is present unless selected in $\Gamma$.

One selected model-bound plugin supplies a base request builder

$$
\beta_\Gamma:
\mathcal X^{*}\times\mathcal Z_1\times\cdots\times\mathcal Z_m
\rightarrow
\mathcal C\times\mathcal Z_1\times\cdots\times\mathcal Z_m.
$$

$\beta_\Gamma$ turns the ledger into a provider-independent draft request. The letter $\beta$ identifies this base-building function. The core does not supply instructions, reminders, capabilities, or compaction while doing so.

When plugin $p_i$ contributes to request preparation, let $f_i$ denote its ordinary successful transformation after it has been connected to the combined plugin state:

$$
f_i:
\mathcal C\times\mathcal Z_1\times\cdots\times\mathcal Z_m
\rightarrow
\mathcal C\times\mathcal Z_1\times\cdots\times\mathcal Z_m.
$$

$f_i$ may change the request and its own state. It leaves state owned by other plugins unchanged. The letter $f$ distinguishes a preparation function from the plugin $p_i$ that supplies it.

For an order-sensitive preparation sequence

$$
\Lambda_P=[p_{i_1},p_{i_2},\ldots,p_{i_r}],
$$

the prepared request is obtained by applying the plugins from left to right. Writing the same composition with functions gives

$$
P_\Gamma
=
f_{i_r}\circ\cdots\circ f_{i_2}\circ f_{i_1}\circ\beta_\Gamma.
$$

Because $g\circ f$ applies $f$ first, $\beta_\Gamma$ builds the draft and $f_{i_1}$ is the first plugin transformation to act. This equation describes the ordinary successful branch; a pause, failure, or cancellation is returned immediately instead of applying later transformations.

## 10. The model boundary

Let

$$
\mathcal S
$$

be the set of execution substrates. A substrate $\sigma\in\mathcal S$ may be a local process, remote service, browser, server, durable worker, or another environment capable of satisfying the required external contracts.

The installed model binding supplies

$$
M_{\Gamma,\sigma}:
\mathcal C
\rightarrow
\mathfrak O(\mathcal O).
$$

Here $\mathcal O$ is the set of normalized model responses. The subscripts $\Gamma$ and $\sigma$ record that the selected plugins choose the model binding and that the concrete call is performed through substrate $\sigma$. The core merely invokes the public boundary.

The core uses only $\mathcal C$ and $\mathcal O$. Provider-specific request and response formats remain inside $M_{\Gamma,\sigma}$.

A normalized response at model-call number $k$ has the form

$$
o_k=
\left(
B_k,
A_k,
y_k
\right).
$$

Its fields are as follows.

- $B_k\in\mathcal X^{*}$ is the complete ordered sequence of items produced by the model binding and accepted for the ledger.
- $A_k\in\mathcal Q^{*}$ is the ordered subsequence of $B_k$ whose structural type is $\mathbf Q$.
- $y_k\in\mathcal Y\sqcup\{\varnothing\}$ is a validated final value or $\varnothing$ when no final value is present.
- $\mathcal Y$ is the set of final values allowed by the installed output protocol.

The model binding must preserve provider output order in $B_k$. It must not classify a response as final merely because the response contains text.

The model call is

$$
o_k=M_{\Gamma,\sigma}(c_k).
$$

The model items are then appended to the ledger:

$$
L_k^{M}
=
L_k\mathbin{\Vert}B_k.
$$

The superscript $M$ labels $L_k^{M}$ as the intermediate ledger after the model response has been recorded.

## 11. Work requests and handlers

Each specific request type is owned by at most one selected handler. A request without a handler is not executed accidentally. It is either converted into a recorded error result by an installed policy or causes the run to fail.

The selected handlers and policies collectively define

$$
D_{\Gamma,\sigma}:
\mathcal Q^{*}\times\mathcal Z_1\times\cdots\times\mathcal Z_m
\rightarrow
\mathfrak O
\left(
\mathcal R^{*}\times\mathcal Q^{*}\times
\mathcal Z_1\times\cdots\times\mathcal Z_m
\right).
$$

For the requests $A_k$, dispatch gives

$$
\left(
V_k,
Q_{k+1},
Z_{k+1}
\right)
=
D_{\Gamma,\sigma}
\left(
A_k,
Z_k^{P}
\right).
$$

Here:

- $V_k\in\mathcal R^{*}$ is the ordered sequence of results already obtained;
- $Q_{k+1}\in\mathcal Q^{*}$ is the ordered sequence of requests still unresolved;
- $Z_{k+1}$ is the plugin state after dispatch.

The ledger after dispatch is

$$
L_{k+1}
=
L_k^{M}\mathbin{\Vert}V_k.
$$

Results are ordered by the corresponding request order unless a selected protocol explicitly defines another deterministic order. Concurrent execution may change when results become available, but it does not silently change their recorded order.

Every $v\in V_k$ must refer to one request $q\in A_k$ through

$$
\rho_v=\iota_q.
$$

One plugin may contribute both a request-preparation function and a request handler. For example, the first contribution can advertise a capability to the model, while the second resolves requests for that capability. The core sees only a request type, its registered handler, and the resulting items.

## 12. The complete active state

At a model-call boundary, the active run state is

$$
\Omega_k=
\left(
L_k,
Q_k,
Z_k,
k
\right).
$$

Its fields are:

- the authoritative ledger $L_k$;
- the unresolved request sequence $Q_k$;
- the combined plugin state $Z_k$;
- the number $k$ of completed model calls before the next call.

For initial user input $u\in\mathcal X$, the initial state is

$$
\Omega_0=
\left(
[u],
[],
Z_0,
0
\right).
$$

$Z_0$ is the collection of initial plugin states declared by $\Gamma$.

The core may begin a model call only when no earlier request remains unresolved:

$$
Q_k=[].
$$

If unresolved requests exist, the run resumes their dispatch before preparing another model request.

## 13. The ordinary transition

Suppose the active state satisfies

$$
Q_k=[].
$$

The ordinary transition proceeds in this order.

First, prepare the model request:

$$
\left(
c_k,
Z_k^{P}
\right)
=
P_\Gamma
\left(
L_k,
Z_k
\right).
$$

Second, obtain a normalized model response:

$$
o_k=M_{\Gamma,\sigma}(c_k).
$$

Third, record its ordered items:

$$
L_k^{M}=L_k\mathbin{\Vert}B_k.
$$

If work was requested, dispatch it:

$$
A_k\ne[]
\Longrightarrow
\left(
V_k,
Q_{k+1},
Z_{k+1}
\right)
=
D_{\Gamma,\sigma}
\left(
A_k,
Z_k^{P}
\right).
$$

Record every completed result:

$$
L_{k+1}=L_k^{M}\mathbin{\Vert}V_k.
$$

If every request has resolved, the next active state is

$$
Q_{k+1}=[]
\Longrightarrow
\Omega_{k+1}
=
\left(
L_{k+1},
[],
Z_{k+1},
k+1
\right).
$$

The core then prepares another model request from the enlarged ledger.

## 14. Completion

A model response completes the run exactly when it contains a validated final value and no work requests:

$$
A_k=[]
\land
y_k\ne\varnothing.
$$

The returned value is

$$
y_k.
$$

If a response contains both a possible final value and one or more requests, the requests take precedence. After their results are recorded, the core proceeds toward model call $k+1$. The possible final value is not returned at that point.

A response with neither a final value nor a request is invalid:

$$
A_k=[]
\land
y_k=\varnothing.
$$

An installed recovery policy may turn that condition into another recorded interaction. Without such a policy, the run fails.

## 15. Pausing and resuming

Dispatch may obtain only some requested results because an approval, user answer, remote callback, or other external value is still required. In that case,

$$
Q_{k+1}\ne[].
$$

The run pauses with the complete resumable state

$$
\Omega_{k+1}
=
\left(
L_{k+1},
Q_{k+1},
Z_{k+1},
k+1
\right).
$$

The state is not a final answer and not a failure. When the missing result arrives, it must identify the request it resolves. The result is appended, the resolved request is removed from $Q_{k+1}$, and dispatch continues. A new model call begins only after the pending sequence becomes empty.

Resuming a pause continues the same application-level turn. It does not insert a new user request or discard the state that led to the pause.

## 16. Failure, cancellation, and unbounded execution

A failure records an unrecovered error, such as:

- an invalid model response;
- a request with no permitted handler;
- malformed plugin output;
- an external operation failure with no recovery policy;
- a violated run policy.

A cancellation records an instruction from outside the active turn to stop it. Cancellation is distinct from completion because it does not claim that the agent produced a valid final value.

The bare core imposes no arbitrary maximum number of model calls. A turn-limit, time-limit, or cost-limit plugin may convert its own threshold crossing into a recorded failure, cancellation, or final value according to its declared policy.

Without such a policy, a run may continue for every $k\in\mathbb N$ and never return. This is unbounded execution, not an implicit failure.

The complete run function is partial:

$$
R_{\Gamma,\sigma}:
\mathcal X
\rightharpoonup
\mathcal Y\sqcup\mathcal U\sqcup\mathcal E\sqcup\mathcal C_A.
$$

The function receives an initial item and may yield a final value, paused state, failure, or cancellation. The partial arrow also permits a run that continues without returning.

## 17. Substrate independence

$\Gamma$ describes logical composition. The substrate $\sigma$ supplies the means to realize the external boundaries required by that composition.

For example, different substrates may provide different implementations of

$$
M_{\Gamma,\sigma}
$$

and

$$
D_{\Gamma,\sigma}.
$$

The same logical plugin may therefore run in a local process, browser, server, or durable worker as long as each substrate satisfies the public contracts it requires.

A plugin that directly assumes a filesystem, network library, clock, database, or model-provider format is not portable across substrates that lack that assumption. Portable logic expresses the need through a public boundary. A substrate binding performs the concrete operation and returns a normalized result.

Substrate independence does not mean that different models or external systems produce equal results. It means the core recurrence and logical composition do not need to change when compatible bindings are replaced.

## 18. Determinism, trace, and replay

Models and external systems may be nondeterministic. The core can still have deterministic replay if their observed outputs are recorded.

Let

$$
E=[e_0,e_1,\ldots,e_n]
$$

be the ordered sequence of external outcomes observed during a run. Let

$$
T=[\Omega_0,\Omega_1,\ldots,\Omega_r]
$$

be the resulting state trace. Here $r$ is the number of recorded state transitions.

If the agent definition, initial state, external outcomes, and all plugin transitions are the same, the trace must be the same:

$$
\Gamma=\Gamma'
\land
\Omega_0=\Omega_0'
\land
E=E'
\Longrightarrow
T=T'.
$$

The primes in $\Gamma'$ and $\Omega_0'$ distinguish the second replay from the first. They do not indicate modification.

A live run need not reproduce another live run because its model or external outcomes may differ. Replay determinism is conditional on using the recorded outcomes.

The trace must record at least:

- the initial state;
- every accepted ledger item;
- every plugin state transition needed for resumption;
- every prepared external request or a reproducible representation of it;
- every external outcome;
- every pause, completion, failure, and cancellation.

Sensitive bodies may be redacted or stored behind references, but the redaction policy must preserve enough structure to explain ordering and resume when resumption is promised.

## 19. Worked composition examples

### 19.1 Instructions and reminders

Suppose $p_s$ contributes instructions and $p_r$ contributes a conditional reminder. Their declared order is

$$
p_s\prec p_r.
$$

On the ordinary successful branch, the request preparation is

$$
P_\Gamma=f_r\circ f_s\circ\beta_\Gamma.
$$

$\beta_\Gamma$ builds the draft request, $f_s$ adds the instruction contribution, and $f_r$ receives the resulting request before adding any reminder. Neither contribution is part of the core.

### 19.2 Context compaction

Suppose $p_c$ compacts a prepared request. If compaction must observe all other ordinary contributions, the configuration declares

$$
p_i\prec p_c
$$

for every other request-preparation plugin $p_i$ whose contribution must be visible to compaction.

The plugin transforms $c_k$ but does not replace $L_k$. On the next model call, another substrate or compaction policy may derive a different request from the same authoritative ledger.

If compaction itself needs an external model call, $p_c$ may pause with private state recording the unfinished transformation. When the external result arrives, the plugin resumes and finishes producing $c_k$. The main agent loop has not yet advanced to $M_{\Gamma,\sigma}$.

### 19.3 A tool-like capability

Suppose a plugin owns the specific request type $\tau_t$. During preparation it adds a capability description to $c_k$. The model later emits request $q$ with

$$
\kappa_q=\mathbf Q
$$

and

$$
\tau_q=\tau_t.
$$

The registered handler produces result $v$ with

$$
\kappa_v=\mathbf R,
\qquad
\tau_v=\tau_t,
\qquad
\rho_v=\iota_q.
$$

$v$ is appended to the ledger. The next model request is prepared from the enlarged ledger, and the model may request more work or provide a final value.

The core never needs a branch dedicated to tools.

### 19.4 Multiple independent requests

Suppose

$$
A_k=[q_1,q_2,q_3].
$$

The substrate may execute their handlers concurrently. If all three succeed, the recorded result sequence is

$$
V_k=[v_1,v_2,v_3],
$$

with

$$
\rho_{v_1}=\iota_{q_1},
\qquad
\rho_{v_2}=\iota_{q_2},
\qquad
\rho_{v_3}=\iota_{q_3}.
$$

Completion time does not alter this order unless the owning protocol explicitly chooses completion order.

If only $q_1$ and $q_3$ have resolved, the run may record their results while retaining

$$
Q_{k+1}=[q_2].
$$

It pauses until a result for $q_2$ arrives or an installed policy resolves the request another way.

### 19.5 A minimal run

Let $u$ be the initial user item. The initial state is

$$
\Omega_0=([u],[],Z_0,0).
$$

Preparation gives

$$
\left(c_0,Z_0^{P}\right)=P_\Gamma([u],Z_0).
$$

The model binding returns

$$
o_0=(B_0,[],y_0).
$$

If

$$
y_0\ne\varnothing,
$$

the run appends $B_0$ and completes with $y_0$. No system prompt, reminder, compaction rule, capability, or turn limit exists unless its plugin was included in $\Gamma$.

## 20. Compact model

The immutable definition is

$$
\Gamma=
\left(
\mathcal P,
\Lambda,
\Pi,
\Theta,
Z_0
\right).
$$

The active state is

$$
\Omega_k=
\left(
L_k,
Q_k,
Z_k,
k
\right).
$$

One model-call cycle is determined by

$$
\left(
c_k,
Z_k^{P}
\right)
=
P_\Gamma
\left(
L_k,
Z_k
\right),
$$

$$
o_k=M_{\Gamma,\sigma}(c_k),
$$

$$
o_k=
\left(
B_k,
A_k,
y_k
\right),
$$

and, when $A_k\ne[]$,

$$
\left(
V_k,
Q_{k+1},
Z_{k+1}
\right)
=
D_{\Gamma,\sigma}
\left(
A_k,
Z_k^{P}
\right).
$$

The run completes when

$$
A_k=[]
\land
y_k\ne\varnothing.
$$

It continues after completed work when

$$
A_k\ne[]
\land
Q_{k+1}=[].
$$

It pauses with resumable state when

$$
Q_{k+1}\ne[].
$$

It fails only through a defined unrecovered error, is cancelled only through an external cancellation, and may continue without a bound when no installed policy supplies one.

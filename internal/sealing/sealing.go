// Package sealing determines whether a captured run can become an immutable
// recording. It deliberately has no persistence or execution dependencies:
// callers may validate a run before committing its manifest.
package sealing

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"reflect"
	"sort"
)

type Completeness string
type Outcome string
type ReplayEligibility string

const (
	Open             Completeness      = "open"
	SealedComplete   Completeness      = "sealed_complete"
	SealedIncomplete Completeness      = "sealed_incomplete"
	OutcomeRunning   Outcome           = "running"
	OutcomeSucceeded Outcome           = "succeeded"
	OutcomeFailed    Outcome           = "failed"
	OutcomeCancelled Outcome           = "cancelled"
	OutcomeUnknown   Outcome           = "unknown"
	ReplayEligible   ReplayEligibility = "eligible"
	ReplayIneligible ReplayEligibility = "ineligible"
)

var (
	ErrSealed              = errors.New("run is already sealed")
	ErrIncomplete          = errors.New("capture is incomplete")
	ErrMissingTerminal     = errors.New("stream has no terminal")
	ErrMissingCausality    = errors.New("event has an unavailable causal parent")
	ErrCausalCycle         = errors.New("causal event graph contains a cycle")
	ErrDuplicateSequence   = errors.New("stream contains duplicate sequence numbers")
	ErrUnmatchedInvocation = errors.New("invocation has unmatched request or response")
	ErrAmbiguousInvocation = errors.New("invocation has ambiguous responses")
)

// Event is the minimal immutable evidence needed by the sealing decision.
// Sequence numbers are scoped to StreamID and events may be added out of order.
type Event struct {
	ID             string   `json:"event_id"`
	StreamID       string   `json:"stream_id"`
	Sequence       int      `json:"sequence"`
	Type           string   `json:"type"`
	ParentEventIDs []string `json:"parent_event_ids,omitempty"`
	InvocationID   string   `json:"invocation_id,omitempty"`
	ReplaySafe     bool     `json:"replay_safe"`
}

type Gap struct {
	StreamID string `json:"stream_id"`
	From     int    `json:"from"`
	To       int    `json:"to"`
	Reason   string `json:"reason"`
}

type Stream struct {
	ID       string `json:"stream_id"`
	Terminal bool   `json:"terminal"`
}

type Run struct {
	ID        string
	ProjectID string
	Events    []Event
	Streams   []Stream
	Gaps      []Gap
	sealed    bool
}

type SealRequest struct {
	AcknowledgeIncomplete bool
}

type Manifest struct {
	SchemaVersion       string            `json:"schema_version"`
	RunID               string            `json:"run_id"`
	ProjectID           string            `json:"project_id"`
	CaptureCompleteness Completeness      `json:"capture_completeness"`
	ExecutionOutcome    Outcome           `json:"execution_outcome"`
	ReplayEligibility   ReplayEligibility `json:"replay_eligibility"`
	EventCount          int               `json:"event_count"`
	StreamCount         int               `json:"stream_count"`
	CaptureGaps         []Gap             `json:"capture_gaps,omitempty"`
	PayloadSHA256       string            `json:"payload_sha256"`
}

type Result struct {
	Manifest Manifest
}

func (r *Run) AddEvent(event Event) error {
	if r.sealed {
		return ErrSealed
	}
	for _, existing := range r.Events {
		if existing.ID == event.ID {
			if !reflect.DeepEqual(existing, event) {
				return fmt.Errorf("event %q conflicts with existing identity", event.ID)
			}
			return nil
		}
	}
	r.Events = append(r.Events, event)
	return nil
}

func (r *Run) AddStream(stream Stream) error {
	if r.sealed {
		return ErrSealed
	}
	for i := range r.Streams {
		if r.Streams[i].ID == stream.ID {
			r.Streams[i] = stream
			return nil
		}
	}
	r.Streams = append(r.Streams, stream)
	return nil
}

func (r *Run) AddGap(gap Gap) error {
	if r.sealed {
		return ErrSealed
	}
	if gap.From < 0 || gap.To < gap.From || gap.Reason == "" {
		return fmt.Errorf("invalid capture gap")
	}
	r.Gaps = append(r.Gaps, gap)
	return nil
}

func (r *Run) Seal(request SealRequest) (Result, error) {
	if r.sealed {
		return Result{}, ErrSealed
	}
	issues, outcome := r.validate()
	if len(issues) > 0 && !request.AcknowledgeIncomplete {
		return Result{}, fmt.Errorf("%w: %s", ErrIncomplete, issues[0])
	}
	r.sealed = true
	completeness := SealedComplete
	if len(issues) > 0 {
		completeness = SealedIncomplete
	}
	eligible := ReplayEligible
	if len(issues) > 0 || !allReplaySafe(r.Events) {
		eligible = ReplayIneligible
	}
	payload, _ := json.Marshal(canonicalEvents(r.Events))
	sum := sha256.Sum256(payload)
	manifest := Manifest{
		SchemaVersion: "1", RunID: r.ID, ProjectID: r.ProjectID,
		CaptureCompleteness: completeness, ExecutionOutcome: outcome,
		ReplayEligibility: eligible, EventCount: len(r.Events),
		StreamCount: len(r.Streams), CaptureGaps: append([]Gap(nil), r.Gaps...),
		PayloadSHA256: hex.EncodeToString(sum[:]),
	}
	return Result{Manifest: manifest}, nil
}

func (r *Run) validate() ([]string, Outcome) {
	var issues []string
	if len(r.Streams) == 0 {
		issues = append(issues, ErrMissingTerminal.Error())
	}
	streams := map[string]bool{}
	for _, stream := range r.Streams {
		streams[stream.ID] = stream.Terminal
	}
	for _, event := range r.Events {
		if event.StreamID == "" || !streams[event.StreamID] {
			issues = append(issues, ErrMissingTerminal.Error())
		}
	}
	for id, terminal := range streams {
		if !terminal {
			issues = append(issues, fmt.Sprintf("%s: %s", id, ErrMissingTerminal))
		}
	}
	seen := map[string]Event{}
	for _, event := range r.Events {
		seen[event.ID] = event
	}
	for _, event := range r.Events {
		for _, parent := range event.ParentEventIDs {
			if _, ok := seen[parent]; !ok {
				issues = append(issues, fmt.Sprintf("%s: %s", event.ID, ErrMissingCausality))
			}
		}
	}
	if hasCycle(r.Events) {
		issues = append(issues, ErrCausalCycle.Error())
	}
	issues = append(issues, invocationIssues(r.Events)...)
	issues = append(issues, gaps(r)...)
	return unique(issues), executionOutcome(r.Events)
}

func gaps(r *Run) []string {
	byStream := map[string][]int{}
	for _, event := range r.Events {
		byStream[event.StreamID] = append(byStream[event.StreamID], event.Sequence)
	}
	var issues []string
	for stream, sequences := range byStream {
		sort.Ints(sequences)
		for i := 1; i < len(sequences); i++ {
			if sequences[i] == sequences[i-1] {
				issues = append(issues, fmt.Sprintf("%s: %s", stream, ErrDuplicateSequence))
				continue
			}
			if sequences[i] > sequences[i-1]+1 {
				issues = append(issues, fmt.Sprintf("%s: missing sequence %d-%d", stream, sequences[i-1]+1, sequences[i]-1))
			}
		}
	}
	for _, gap := range r.Gaps {
		issues = append(issues, fmt.Sprintf("%s: capture gap %d-%d", gap.StreamID, gap.From, gap.To))
	}
	return issues
}

func invocationIssues(events []Event) []string {
	requested := map[string]int{}
	completed := map[string]int{}
	for _, event := range events {
		switch event.Type {
		case "model.requested", "tool.requested":
			requested[event.InvocationID]++
		case "model.completed", "model.failed", "tool.completed", "tool.failed":
			completed[event.InvocationID]++
		}
	}
	var issues []string
	for id, count := range requested {
		if id == "" || count != completed[id] {
			issues = append(issues, fmt.Sprintf("%s: %s", id, ErrUnmatchedInvocation))
		}
	}
	for id, count := range completed {
		if requested[id] == 0 || count > requested[id] {
			issues = append(issues, fmt.Sprintf("%s: %s", id, ErrAmbiguousInvocation))
		}
	}
	return issues
}

func executionOutcome(events []Event) Outcome {
	for _, event := range events {
		switch event.Type {
		case "run.failed":
			return OutcomeFailed
		case "run.cancelled":
			return OutcomeCancelled
		case "run.completed":
			return OutcomeSucceeded
		}
	}
	return OutcomeUnknown
}

func hasCycle(events []Event) bool {
	parents := map[string][]string{}
	for _, event := range events {
		parents[event.ID] = event.ParentEventIDs
	}
	visiting, visited := map[string]bool{}, map[string]bool{}
	var visit func(string) bool
	visit = func(id string) bool {
		if visiting[id] {
			return true
		}
		if visited[id] {
			return false
		}
		visiting[id] = true
		for _, parent := range parents[id] {
			if visit(parent) {
				return true
			}
		}
		delete(visiting, id)
		visited[id] = true
		return false
	}
	for id := range parents {
		if visit(id) {
			return true
		}
	}
	return false
}

func allReplaySafe(events []Event) bool {
	for _, event := range events {
		if !event.ReplaySafe {
			return false
		}
	}
	return true
}

func canonicalEvents(events []Event) []Event {
	copyEvents := append([]Event(nil), events...)
	sort.Slice(copyEvents, func(i, j int) bool {
		if copyEvents[i].StreamID != copyEvents[j].StreamID {
			return copyEvents[i].StreamID < copyEvents[j].StreamID
		}
		if copyEvents[i].Sequence != copyEvents[j].Sequence {
			return copyEvents[i].Sequence < copyEvents[j].Sequence
		}
		return copyEvents[i].ID < copyEvents[j].ID
	})
	return copyEvents
}

func unique(items []string) []string {
	seen := map[string]bool{}
	out := make([]string, 0, len(items))
	for _, item := range items {
		if !seen[item] {
			seen[item] = true
			out = append(out, item)
		}
	}
	return out
}

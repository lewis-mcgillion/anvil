package contracts

import (
	"bufio"
	"os"
	"path/filepath"
	"regexp"
	"sort"
	"strconv"
	"strings"
	"testing"
)

func protoSource(t *testing.T) string {
	t.Helper()
	data, err := os.ReadFile(filepath.Join(repoRoot(t), "proto", "anvil", "runner", "v1", "runner.proto"))
	if err != nil {
		t.Fatal(err)
	}
	return string(data)
}

// TestRunnerProtocolMessagesExist pins the message set from
// docs/architecture.md: RunnerHello, EmitEvents, ModelRequest,
// ModelResponse, ToolRequest, ToolResponse, Heartbeat, RunnerFinished.
func TestRunnerProtocolMessagesExist(t *testing.T) {
	src := protoSource(t)
	for _, msg := range []string{
		"RunnerHello", "EmitEvents", "ModelRequest", "ModelResponse",
		"ToolRequest", "ToolResponse", "Heartbeat", "RunnerFinished",
		"RunnerMessage", "SupervisorMessage", "Error", "Budgets", "Cancel",
	} {
		if !regexp.MustCompile(`message ` + msg + `\b`).MatchString(src) {
			t.Errorf("protocol message %s missing from runner.proto", msg)
		}
	}
}

// TestNoArbitraryExecutionPrimitive is the core safety contract: the
// runner protocol must not expose any primitive for arbitrary commands,
// URLs, filesystem access, environment manipulation or original-tool
// invocation. Model and tool traffic only flows through the typed
// request/response pairs satisfied from fixtures (or the opt-in broker).
func TestNoArbitraryExecutionPrimitive(t *testing.T) {
	src := strings.ToLower(protoSource(t))
	banned := []string{
		"exec", "command", "shell", "subprocess", "spawn",
		"url", "uri", "http", "https", "endpoint",
		"file_path", "filepath", "path =", "filesystem",
		"env", "secret", "credential",
		"original_tool", "production",
	}
	// Strip comments before scanning for banned field/identifier names.
	var code strings.Builder
	sc := bufio.NewScanner(strings.NewReader(src))
	for sc.Scan() {
		line := sc.Text()
		if i := strings.Index(line, "//"); i >= 0 {
			line = line[:i]
		}
		code.WriteString(line)
		code.WriteByte('\n')
	}
	stripped := code.String()
	fieldRE := regexp.MustCompile(`(?:^|\s)(?:repeated\s+)?[A-Za-z0-9_.]+\s+([a-z_][a-z0-9_]*)\s*=\s*\d+`)
	for _, m := range fieldRE.FindAllStringSubmatch(stripped, -1) {
		name := m[1]
		for _, b := range banned {
			if strings.Contains(name, b) {
				t.Errorf("field name %q contains banned token %q: no arbitrary-execution surface allowed", name, b)
			}
		}
	}
	// ToolRequest must carry canonical arguments and a schema digest only.
	toolReq := stripped[strings.Index(stripped, "message toolrequest"):]
	toolReq = toolReq[:strings.Index(toolReq, "}")]
	for _, want := range []string{"tool_name", "schema_digest", "arguments", "invocation_id", "request_id"} {
		if !strings.Contains(toolReq, want) {
			t.Errorf("ToolRequest must contain %s (typed fixture matching only)", want)
		}
	}
	// Playback never schedules execution: the protocol has no
	// "playback" -> execution bridge message at all.
	if strings.Contains(stripped, "playback") {
		t.Error("runner protocol must not reference playback; playback is read-only REST only")
	}
}

// fieldTag is a (message, field, number) triple used for the stability
// golden: proto numbers may never be renumbered or reused.
type fieldTag struct {
	Message string
	Field   string
	Number  int
}

func parseFieldTags(t *testing.T) []fieldTag {
	t.Helper()
	src := protoSource(t)
	var tags []fieldTag
	msgRE := regexp.MustCompile(`^message ([A-Za-z0-9]+) \{`)
	fieldRE := regexp.MustCompile(`^\s*(?:repeated\s+)?[A-Za-z0-9_.]+\s+([a-z_][a-z0-9_]*)\s*=\s*(\d+)`)
	oneofRE := regexp.MustCompile(`^\s*([A-Za-z0-9]+)\s+([a-z_][a-z0-9_]*)\s*=\s*(\d+)`)
	var stack []string
	sc := bufio.NewScanner(strings.NewReader(src))
	for sc.Scan() {
		line := sc.Text()
		if i := strings.Index(line, "//"); i >= 0 {
			line = line[:i]
		}
		if m := msgRE.FindStringSubmatch(line); m != nil {
			stack = append(stack, m[1])
			continue
		}
		if strings.Contains(line, "}") && len(stack) > 0 && !strings.Contains(line, "{") {
			// crude nesting tracking: pop on lines that are just "}"
			if strings.TrimSpace(line) == "}" {
				stack = stack[:len(stack)-1]
				continue
			}
		}
		if len(stack) == 0 {
			continue
		}
		if strings.Contains(line, "oneof") {
			continue
		}
		var name string
		var numStr string
		if m := fieldRE.FindStringSubmatch(line); m != nil {
			name, numStr = m[1], m[2]
		} else if m := oneofRE.FindStringSubmatch(line); m != nil {
			name, numStr = m[2], m[3]
		} else {
			continue
		}
		n, err := strconv.Atoi(numStr)
		if err != nil {
			continue
		}
		tags = append(tags, fieldTag{Message: stack[len(stack)-1], Field: name, Number: n})
	}
	sort.Slice(tags, func(i, j int) bool {
		if tags[i].Message != tags[j].Message {
			return tags[i].Message < tags[j].Message
		}
		return tags[i].Number < tags[j].Number
	})
	return tags
}

const protoGoldenFile = "testdata/runner_proto_tags.golden"

// TestProtoFieldTagsStable is the regression guard for wire compatibility:
// every existing (message, field, number) triple must remain. New fields
// may be appended with fresh numbers; run with UPDATE_GOLDEN=1 after an
// intentional, reviewed contract change (and update docs/compatibility.md).
func TestProtoFieldTagsStable(t *testing.T) {
	tags := parseFieldTags(t)
	var sb strings.Builder
	for _, tg := range tags {
		sb.WriteString(tg.Message)
		sb.WriteByte('.')
		sb.WriteString(tg.Field)
		sb.WriteByte('=')
		sb.WriteString(strconv.Itoa(tg.Number))
		sb.WriteByte('\n')
	}
	got := sb.String()
	path := filepath.Join(repoRoot(t), "internal", "contracts", protoGoldenFile)
	if os.Getenv("UPDATE_GOLDEN") == "1" {
		if err := os.WriteFile(path, []byte(got), 0o644); err != nil {
			t.Fatal(err)
		}
		t.Skip("golden updated")
	}
	want, err := os.ReadFile(path)
	if err != nil {
		t.Fatalf("read golden (create with UPDATE_GOLDEN=1): %v", err)
	}
	if got != string(want) {
		t.Errorf("proto field layout changed.\n--- want (golden) ---\n%s\n--- got ---\n%s\nField renumbering is a breaking change; see docs/compatibility.md", string(want), got)
	}
}

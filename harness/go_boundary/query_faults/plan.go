// Package query_faults implements explicit, bounded query faults for local acceptance.
// It lives in the runner-owned go_boundary module (harness/go_boundary)
// and is compiled against the selected, unmodified Gonka; it is never linked
// into a node.
package query_faults

import (
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"

	wasmkeeper "github.com/CosmWasm/wasmd/x/wasm/keeper"
	vm "github.com/CosmWasm/wasmvm/v2/types"
	sdk "github.com/cosmos/cosmos-sdk/types"
	"github.com/cosmos/cosmos-sdk/types/bech32"
	"github.com/cosmos/gogoproto/proto"
	"github.com/productscience/inference/x/inference/types"
)

const Summary = "/inference.inference.Query/EpochPerformanceSummaryByParticipant"
const Routing = "/inference.inference.Query/ListClaimRecipients"
const CurrentEpoch = "/inference.inference.Query/GetCurrentEpoch"
const MaxPlanBytes = 65536

type Plan struct {
	Version int    `json:"version"`
	ChainID string `json:"chain_id"`
	Rules   []Rule `json:"rules"`
}
type Rule struct {
	ID          string `json:"id"`
	Deal        string `json:"deal"`
	Route       string `json:"route"`
	Host        string `json:"host"`
	Epoch       uint64 `json:"epoch"`
	FromHeight  int64  `json:"from_height"`
	UntilHeight int64  `json:"until_height"`
	Kind        string `json:"kind"`
	OtherHost   string `json:"other_host,omitempty"`
}

func validAddress(s string) bool {
	hrp, raw, err := bech32.DecodeAndConvert(s)
	return err == nil && hrp == "gonka" && (len(raw) == 20 || len(raw) == 32)
}

func Load(reader io.Reader) (Plan, string, error) {
	data, err := io.ReadAll(io.LimitReader(reader, MaxPlanBytes+1))
	if err != nil {
		return Plan{}, "", err
	}
	if len(data) > MaxPlanBytes {
		return Plan{}, "", errors.New("plan exceeds 64 KiB")
	}
	var p Plan
	d := json.NewDecoder(bytes.NewReader(data))
	d.DisallowUnknownFields()
	if err := d.Decode(&p); err != nil {
		return p, "", err
	}
	if err := d.Decode(new(any)); err != io.EOF {
		return p, "", errors.New("trailing plan data")
	}
	if err := p.Validate(); err != nil {
		return p, "", err
	}
	hash := sha256.Sum256(data)
	return p, hex.EncodeToString(hash[:]), nil
}

func (p Plan) Validate() error {
	if p.Version != 1 || p.ChainID == "" || len(p.Rules) == 0 || len(p.Rules) > 64 {
		return errors.New("invalid plan version, chain or rule count")
	}
	ids := map[string]bool{}
	for i, r := range p.Rules {
		if r.ID == "" || ids[r.ID] || !validAddress(r.Deal) || !validAddress(r.Host) || r.FromHeight <= 0 || r.UntilHeight <= r.FromHeight {
			return fmt.Errorf("invalid scope for rule %d", i)
		}
		ids[r.ID] = true
		if r.Route != Summary && r.Route != Routing && r.Route != CurrentEpoch {
			return fmt.Errorf("unsupported route in %s", r.ID)
		}
		switch r.Kind {
		case "handler_error", "unsupported_request", "malformed_protobuf", "oversized_response":
		case "missing_nested_summary", "wrong_host", "wrong_epoch", "invalid_participant_address":
			if r.Route != Summary {
				return fmt.Errorf("summary-only fault %s", r.ID)
			}
		case "duplicate_routing":
			if r.Route != Routing {
				return fmt.Errorf("routing-only fault %s", r.ID)
			}
		default:
			return fmt.Errorf("unsupported fault kind %s", r.Kind)
		}
		if r.Kind == "wrong_host" && (!validAddress(r.OtherHost) || r.OtherHost == r.Host) {
			return errors.New("wrong_host requires a valid different address")
		}
		if r.Kind == "wrong_epoch" && r.Epoch == ^uint64(0) {
			return errors.New("wrong_epoch overflow")
		}
		for _, prior := range p.Rules[:i] {
			// Routing/current-epoch requests do not contain E. Treat overlapping
			// windows for the same caller+route as ambiguous regardless of E.
			if prior.Deal == r.Deal && prior.Route == r.Route && prior.FromHeight < r.UntilHeight && r.FromHeight < prior.UntilHeight {
				return errors.New("overlapping caller/route fault windows")
			}
		}
	}
	return nil
}

func (r Rule) matches(q *vm.GrpcQuery, caller sdk.AccAddress, height int64) bool {
	if q == nil || q.Path != r.Route || caller.String() != r.Deal || height < r.FromHeight || height >= r.UntilHeight {
		return false
	}
	switch r.Route {
	case Summary:
		var req types.QueryEpochPerformanceSummaryByParticipantRequest
		return proto.Unmarshal(q.Data, &req) == nil && req.ParticipantId == r.Host && req.EpochIndex == r.Epoch
	case Routing:
		var req types.QueryListClaimRecipientsRequest
		return proto.Unmarshal(q.Data, &req) == nil && req.Participant == r.Host
	case CurrentEpoch:
		var req types.QueryGetCurrentEpochRequest
		return proto.Unmarshal(q.Data, &req) == nil
	}
	return false
}

func (r Rule) response() ([]byte, error) {
	switch r.Kind {
	case "handler_error":
		return nil, errors.New("a8 injected query handler failure")
	case "unsupported_request":
		return nil, vm.UnsupportedRequest{Kind: "a8 scoped query fault"}
	case "malformed_protobuf":
		return []byte{0xff}, nil
	case "oversized_response":
		return bytes.Repeat([]byte{0}, 32769), nil
	case "missing_nested_summary":
		return []byte{}, nil
	case "duplicate_routing":
		entry := types.ClaimRecipientEntry{Epoch: r.Epoch, Recipient: r.Deal}
		return proto.Marshal(&types.QueryListClaimRecipientsResponse{Entries: []types.ClaimRecipientEntry{entry, entry}})
	default:
		summary := types.EpochPerformanceSummary{EpochIndex: r.Epoch, ParticipantId: r.Host, Claimed: true}
		switch r.Kind {
		case "wrong_host":
			summary.ParticipantId = r.OtherHost
		case "wrong_epoch":
			summary.EpochIndex++
		case "invalid_participant_address":
			summary.ParticipantId = "not-a-gonka-address"
		default:
			return nil, errors.New("invalid a8 fault kind")
		}
		return proto.Marshal(&types.QueryEpochPerformanceSummaryByParticipantResponse{EpochPerformanceSummary: summary})
	}
}

// Decorate copies the plan. A caller cannot change a running node's behavior by
// mutating the original slice. No file reads, network calls, state writes or gas
// recovery occur here; errors/panics from the delegate retain normal semantics.
func Decorate(p Plan, next wasmkeeper.WasmVMQueryHandler) wasmkeeper.WasmVMQueryHandler {
	if err := p.Validate(); err != nil {
		panic(err)
	}
	rules := append([]Rule(nil), p.Rules...)
	chainID := p.ChainID
	return wasmkeeper.WasmVMQueryHandlerFn(func(ctx sdk.Context, caller sdk.AccAddress, q vm.QueryRequest) ([]byte, error) {
		if ctx.ChainID() == chainID {
			for _, r := range rules {
				if r.matches(q.Grpc, caller, ctx.BlockHeight()) {
					return r.response()
				}
			}
		}
		return next.HandleQuery(ctx, caller, q)
	})
}

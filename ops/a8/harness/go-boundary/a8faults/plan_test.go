package a8faults

import (
	"bytes"
	"encoding/json"
	"reflect"
	"testing"

	wasmkeeper "github.com/CosmWasm/wasmd/x/wasm/keeper"
	vm "github.com/CosmWasm/wasmvm/v2/types"
	sdk "github.com/cosmos/cosmos-sdk/types"
	"github.com/cosmos/cosmos-sdk/types/bech32"
	"github.com/cosmos/gogoproto/proto"
	"github.com/productscience/inference/x/inference/types"
	"github.com/stretchr/testify/require"
)

func fixture(t *testing.T) (Plan, sdk.AccAddress, vm.QueryRequest) {
	t.Helper()
	caller := sdk.AccAddress(bytes.Repeat([]byte{1}, 32))
	// sdk.Context caller.String uses the app's configured prefix. Standalone
	// provider tests set it exactly as the node does.
	sdk.GetConfig().SetBech32PrefixForAccount("gonka", "gonkapub")
	host, err := bech32.ConvertAndEncode("gonka", bytes.Repeat([]byte{2}, 20))
	require.NoError(t, err)
	r := Rule{ID: "summary", Deal: caller.String(), Host: host, Epoch: 5, Route: Summary, FromHeight: 100, UntilHeight: 200, Kind: "handler_error"}
	data, err := proto.Marshal(&types.QueryEpochPerformanceSummaryByParticipantRequest{ParticipantId: host, EpochIndex: 5})
	require.NoError(t, err)
	return Plan{Version: 1, ChainID: "test-chain", Rules: []Rule{r}}, caller, vm.QueryRequest{Grpc: &vm.GrpcQuery{Path: Summary, Data: data}}
}

func requireSystemErrorField(t *testing.T, systemErr *vm.SystemError, fieldName string) {
	t.Helper()
	require.NotNil(t, systemErr)
	tv := reflect.ValueOf(systemErr)
	require.Equal(t, reflect.Ptr, tv.Kind())
	v := tv.Elem()
	f := v.FieldByName(fieldName)
	require.True(t, f.IsValid(), "missing system error field %s", fieldName)
	require.NotZero(t, f.Interface())
}

func TestIsolationRecoveryAndImmutablePlan(t *testing.T) {
	p, caller, request := fixture(t)
	delegated := 0
	next := wasmkeeper.WasmVMQueryHandlerFn(func(_ sdk.Context, _ sdk.AccAddress, _ vm.QueryRequest) ([]byte, error) {
		delegated++
		return []byte("healthy"), nil
	})
	h := Decorate(p, next)
	p.Rules[0].Kind = "malformed_protobuf"
	ctx := sdk.Context{}.WithChainID("test-chain")
	for _, height := range []int64{99, 200, 201} {
		out, err := h.HandleQuery(ctx.WithBlockHeight(height), caller, request)
		require.NoError(t, err)
		require.Equal(t, []byte("healthy"), out)
	}
	for _, height := range []int64{100, 199} {
		_, err := h.HandleQuery(ctx.WithBlockHeight(height), caller, request)
		require.ErrorContains(t, err, "handler failure")
	}
	_, err := h.HandleQuery(ctx.WithBlockHeight(150).WithChainID("other"), caller, request)
	require.NoError(t, err)
	_, err = h.HandleQuery(ctx.WithBlockHeight(150), sdk.AccAddress(bytes.Repeat([]byte{3}, 32)), request)
	require.NoError(t, err)
	for _, q := range []vm.QueryRequest{
		{Grpc: &vm.GrpcQuery{Path: Routing}},
		{Grpc: &vm.GrpcQuery{Path: Summary, Data: []byte{0xff}}},
		{},
	} {
		_, err = h.HandleQuery(ctx.WithBlockHeight(150), caller, q)
		require.NoError(t, err)
	}
	for _, req := range []types.QueryEpochPerformanceSummaryByParticipantRequest{
		{ParticipantId: p.Rules[0].Host, EpochIndex: 6}, {ParticipantId: "another", EpochIndex: 5},
	} {
		data, e := proto.Marshal(&req)
		require.NoError(t, e)
		_, err = h.HandleQuery(ctx.WithBlockHeight(150), caller, vm.QueryRequest{Grpc: &vm.GrpcQuery{Path: Summary, Data: data}})
		require.NoError(t, err)
	}
	require.Equal(t, 10, delegated)
}

func TestPayloadsAndGoWasmBoundary(t *testing.T) {
	p, caller, q := fixture(t)
	other, err := bech32.ConvertAndEncode("gonka", bytes.Repeat([]byte{3}, 20))
	require.NoError(t, err)
	for _, kind := range []string{"handler_error", "unsupported_request", "malformed_protobuf", "oversized_response", "missing_nested_summary", "wrong_host", "wrong_epoch", "invalid_participant_address"} {
		t.Run(kind, func(t *testing.T) {
			p.Rules[0].Kind = kind
			p.Rules[0].OtherHost = other
			h := Decorate(p, wasmkeeper.WasmVMQueryHandlerFn(func(sdk.Context, sdk.AccAddress, vm.QueryRequest) ([]byte, error) {
				t.Fatal("unexpected delegation")
				return nil, nil
			}))
			out, e := h.HandleQuery(sdk.Context{}.WithChainID(p.ChainID).WithBlockHeight(100), caller, q)
			result := vm.ToQuerierResult(out, e)
			switch kind {
			case "handler_error":
				require.NotEmpty(t, result.Ok.Err)
			case "unsupported_request":
				require.NotNil(t, result.Err.UnsupportedRequest)
			case "missing_nested_summary":
				require.Empty(t, out)
				require.NoError(t, e)
			case "oversized_response":
				require.Len(t, out, 32769)
			case "malformed_protobuf":
				require.Equal(t, []byte{0xff}, out)
			default:
				var decoded types.QueryEpochPerformanceSummaryByParticipantResponse
				require.NoError(t, proto.Unmarshal(out, &decoded))
				require.NoError(t, e)
				if kind == "wrong_host" {
					require.Equal(t, other, decoded.EpochPerformanceSummary.ParticipantId)
				}
				if kind == "wrong_epoch" {
					require.EqualValues(t, 6, decoded.EpochPerformanceSummary.EpochIndex)
				}
				if kind == "invalid_participant_address" {
					require.False(t, validAddress(decoded.EpochPerformanceSummary.ParticipantId))
				}
			}
		})
	}
}

func TestToQuerierResultClassifiesVMSystemErrors(t *testing.T) {
	for _, tc := range []struct {
		name  string
		err   error
		field string
	}{
		{name: "InvalidResponse", err: vm.InvalidResponse{}, field: "InvalidResponse"},
		{name: "InvalidRequest", err: vm.InvalidRequest{}, field: "InvalidRequest"},
		{name: "Unknown", err: vm.Unknown{}, field: "Unknown"},
		{name: "NoSuchContract", err: vm.NoSuchContract{}, field: "NoSuchContract"},
		{name: "NoSuchCode", err: vm.NoSuchCode{}, field: "NoSuchCode"},
	} {
		t.Run(tc.name, func(t *testing.T) {
			result := vm.ToQuerierResult(nil, tc.err)
			require.Nil(t, result.Ok)
			require.NotNil(t, result.Err)
			requireSystemErrorField(t, result.Err, tc.field)
			// Go classification/serialization only; the compiled-Wasm ABI
			// decoder is exercised separately by test_wasm_query_boundary.mjs.
			encoded, err := json.Marshal(result)
			require.NoError(t, err)
			var decoded vm.QuerierResult
			require.NoError(t, json.Unmarshal(encoded, &decoded))
			require.Nil(t, decoded.Ok)
			requireSystemErrorField(t, decoded.Err, tc.field)

		})
	}
}

func TestRoutingAndEpochScopes(t *testing.T) {
	p, caller, _ := fixture(t)
	for _, route := range []string{Routing, CurrentEpoch} {
		p.Rules[0].Route = route
		p.Rules[0].Kind = "handler_error"
		var data []byte
		if route == Routing {
			data, _ = proto.Marshal(&types.QueryListClaimRecipientsRequest{Participant: p.Rules[0].Host})
		}
		r := p.Rules[0]
		require.True(t, r.matches(&vm.GrpcQuery{Path: route, Data: data}, caller, 100))
		require.False(t, r.matches(&vm.GrpcQuery{Path: route, Data: []byte{0xff}}, caller, 100))
		require.False(t, r.matches(&vm.GrpcQuery{Path: route, Data: data}, sdk.AccAddress([]byte("different")), 100))
	}
	p.Rules[0].Route = Routing
	p.Rules[0].Kind = "duplicate_routing"
	require.NoError(t, p.Validate())
	data, err := p.Rules[0].response()
	require.NoError(t, err)
	var decoded types.QueryListClaimRecipientsResponse
	require.NoError(t, proto.Unmarshal(data, &decoded))
	require.Len(t, decoded.Entries, 2)
	require.Equal(t, decoded.Entries[0], decoded.Entries[1])
}

func TestStrictPlanValidation(t *testing.T) {
	p, _, _ := fixture(t)
	data, err := json.Marshal(p)
	require.NoError(t, err)
	_, digest, err := Load(bytes.NewReader(data))
	require.NoError(t, err)
	require.Len(t, digest, 64)
	for _, invalid := range [][]byte{append(data, []byte("{}")...), bytes.Repeat([]byte("x"), MaxPlanBytes+1), []byte(`{"version":1,"unknown":true}`)} {
		_, _, err = Load(bytes.NewReader(invalid))
		require.Error(t, err)
	}
	for _, change := range []func(*Rule){
		func(r *Rule) { r.Deal = "*" }, func(r *Rule) { r.Host = "*" }, func(r *Rule) { r.UntilHeight = r.FromHeight },
		func(r *Rule) { r.Kind = "invalid_response_envelope" }, func(r *Rule) { r.Kind = "invalid_request" },
		func(r *Rule) { r.Kind = "unknown" }, func(r *Rule) { r.Kind = "no_such_contract" },
		func(r *Rule) { r.Kind = "no_such_code" }, func(r *Rule) { r.Route = "/other" },
		func(r *Rule) { r.Kind = "wrong_host"; r.OtherHost = r.Host },
	} {
		bad := p
		bad.Rules = append([]Rule(nil), p.Rules...)
		change(&bad.Rules[0])
		require.Error(t, bad.Validate())
	}
	p.Rules = append(p.Rules, p.Rules[0])
	p.Rules[1].ID = "overlap"
	require.Error(t, p.Validate())
}

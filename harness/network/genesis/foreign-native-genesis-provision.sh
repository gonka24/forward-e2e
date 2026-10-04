#!/bin/sh
# External genesis provisioner (runner-owned; never part of Gonka).
#
# Derived step by step from Gonka inference-chain/scripts/init-docker-genesis.sh
#   at commit c33c9eaa5bc40c53b564159b5e1534bbfdab8a08
#   sha256 02355d2ca35647c8ea92deefc4ddd0ca2ee02e3b5f8d4efee6c13d2a8fa8dd1e
# The runner (scripts/external_harness.py, GENESIS_PROVISIONER_UPSTREAM_SHA256)
# refuses foreign-native-preservation with GENESIS_PROVISIONER_INCOMPATIBLE when the selected Gonka's
# script has any other hash: a changed upstream sequence must be re-derived by a
# person, never auto-adapted.
#
# Differences from the upstream script, and nothing else:
#   1. the foreign-native fixture account is added with the standard
#      `inferenced genesis add-genesis-account` command immediately after the
#      POOL account line, so Bank supply stays equal to genesis balances;
#   2. the genesis immediately before and after that command, and the final
#      genesis, are saved with their sha256 and the exact command under
#      $STATE_DIR/a8-provision (prod-local/genesis/a8-provision on the host);
#   3. `inferenced genesis validate` runs on the final genesis before the chain
#      is started.
#   4. shell tracing is disabled while checking/consuming the optional tgbot
#      key password so it cannot be copied into container logs.
#   5. shell syntax uses /bin/sh, available in the Gonka runtime image:
#      function variables are isolated in a subshell and CONFIG_ key replacement
#      uses sed instead of Bash parameter substitution.
# It is started by local-test-net/foreign-native-genesis.yml from /a8-provision, a
# separate path: /root/init-docker-genesis.sh in the image is not touched.
set -e
set -x

A8_PROVISIONER_DERIVED_FROM_SHA256="02355d2ca35647c8ea92deefc4ddd0ca2ee02e3b5f8d4efee6c13d2a8fa8dd1e"

filter_cw20_code() {
  input=$(cat)
  # Remove cw20_code field and its value using sed
  echo "$input" | sed -n -E '
    # If we find cw20_code, skip until the next closing brace
    /[[:space:]]*"cw20_code":[[:space:]]*"/ {
      :skip
      n
      /^[[:space:]]*}[,}]?$/! b skip
      n
    }
    # Print all other lines
    p
  '
}

if [ -z "$KEYRING_BACKEND" ]; then
  echo "KEYRING_BACKEND is not specified defaulting to test"
  KEYRING_BACKEND="test"
fi

# A8: the provisioner exists only for the B3 fixture; fail closed without it.
: "${A8_B3_FOREIGN_ADDRESS:?A8_B3_FOREIGN_ADDRESS is required by the A8 provisioner}"
: "${A8_B3_FOREIGN_DENOM:?A8_B3_FOREIGN_DENOM is required by the A8 provisioner}"
: "${A8_B3_FOREIGN_AMOUNT:?A8_B3_FOREIGN_AMOUNT is required by the A8 provisioner}"
case "$A8_B3_FOREIGN_AMOUNT" in
  0|*[!0-9]*|'')
    echo "A8_B3_FOREIGN_AMOUNT must be a positive integer" >&2
    exit 1
    ;;
esac

# Display the parsed values (for debugging)
echo "Using the following arguments"
echo "KEYRING_BACKEND: $KEYRING_BACKEND"

KEY_NAME="genesis"
APP_NAME="inferenced"
CHAIN_ID="gonka-mainnet"
COIN_DENOM="ngonka"
STATE_DIR="/root/.inference"

if [ "$A8_B3_FOREIGN_DENOM" = "$COIN_DENOM" ]; then
  echo "A8_B3_FOREIGN_DENOM must differ from $COIN_DENOM" >&2
  exit 1
fi

update_configs() {
  if [ "${REST_API_ACTIVE:-}" = true ]; then
    "$APP_NAME" patch-toml "$STATE_DIR/config/app.toml" app_overrides.toml
  else
    echo "Skipping update node config"
  fi
}


# Init the chain:
# I'm using prod-sim as the chain name (production simulation)
#   and icoin (intelligence coin) as the default denomination
#   and my-node as a node moniker (it doesn't have to be unique)
output=$($APP_NAME init \
  --chain-id "$CHAIN_ID" \
  --default-denom $COIN_DENOM \
  my-node 2>&1)
exit_code=$?
if [ $exit_code -ne 0 ]; then
    echo "Error: '$APP_NAME init' failed with exit code $exit_code"
    echo "Output:"
    echo "$output"
    exit $exit_code
fi
echo "$output" | filter_cw20_code

echo "Setting the chain configuration"

SNAPSHOT_INTERVAL=${SNAPSHOT_INTERVAL:-10}
SNAPSHOT_KEEP_RECENT=${SNAPSHOT_KEEP_RECENT:-5}

$APP_NAME config set client chain-id $CHAIN_ID
$APP_NAME config set client keyring-backend $KEYRING_BACKEND
$APP_NAME config set app minimum-gas-prices "0$COIN_DENOM"
$APP_NAME config set app state-sync.snapshot-interval $SNAPSHOT_INTERVAL
$APP_NAME config set app state-sync.snapshot-keep-recent $SNAPSHOT_KEEP_RECENT

echo "Setting the node configuration (config.toml)"
if [ -n "$P2P_EXTERNAL_ADDRESS" ]; then
  echo "Setting the external address for P2P to $P2P_EXTERNAL_ADDRESS"
  $APP_NAME config set config p2p.external_address "$P2P_EXTERNAL_ADDRESS" --skip-validate
else
  echo "P2P_EXTERNAL_ADDRESS is not set, skipping"
fi

sed -Ei 's/^laddr = ".*:26657"$/laddr = "tcp:\/\/0\.0\.0\.0:26657"/g' \
  $STATE_DIR/config/config.toml
# no seeds for genesis node
sed -Ei "s/^seeds = .*$/seeds = \"\"/g" \
  $STATE_DIR/config/config.toml
#sed -Ei 's/^log_level = "info"$/log_level = "debug"/g' $STATE_DIR/config/config.toml
#if [ -n "${DEBUG-}" ]; then
#  sed -i 's/^log_level = "info"/log_level = "debug"/' "$STATE_DIR/config/config.toml"
#fi


echo "Creating the key"
# Create a key
$APP_NAME keys \
    --keyring-backend $KEYRING_BACKEND --keyring-dir "$STATE_DIR" \
    add "$KEY_NAME"
$APP_NAME keys \
    --keyring-backend $KEYRING_BACKEND --keyring-dir "$STATE_DIR" \
    add "POOL_product_science_inc"

# Create warm key for ML operations
KEY_NAME_WARM="${KEY_NAME}_warm"
$APP_NAME keys \
    --keyring-backend $KEYRING_BACKEND --keyring-dir "$STATE_DIR" \
    add "$KEY_NAME_WARM"

modify_genesis_file() (
  json_file="$HOME/.inference/config/genesis.json"
  override_file="$1"


  if [ ! -f "$override_file" ]; then
    echo "Override file $override_file does not exist. Exiting..."
    return
  fi
  echo "Checking if jq is installed"
  which jq
  jq ". * input" "$json_file" "$override_file" > "${json_file}.tmp"
  mv "${json_file}.tmp" "$json_file"
  echo "Modified $json_file with file: $override_file"
  cat "$json_file" | filter_cw20_code
)

# Usage
modify_genesis_file 'denom.json'
MILLION_BASE="000000$COIN_DENOM"
NATIVE="000000000$COIN_DENOM"
MILLION_NATIVE="000000$NATIVE"

echo "Adding the keys to the genesis account"
$APP_NAME genesis add-genesis-account "$KEY_NAME" "2$NATIVE" --keyring-backend $KEYRING_BACKEND
$APP_NAME genesis add-genesis-account "POOL_product_science_inc" "160$MILLION_NATIVE" --keyring-backend $KEYRING_BACKEND

# --- A8 B3 fixture (difference 1 and 2) -------------------------------------
A8_PROVISION_DIR="$STATE_DIR/a8-provision"
A8_GENESIS_FILE="$STATE_DIR/config/genesis.json"
mkdir -p "$A8_PROVISION_DIR"
cp "$A8_GENESIS_FILE" "$A8_PROVISION_DIR/genesis-before-b3.json"
A8_B3_COIN="${A8_B3_FOREIGN_AMOUNT}${A8_B3_FOREIGN_DENOM}"
A8_B3_COMMAND="$APP_NAME genesis add-genesis-account $A8_B3_FOREIGN_ADDRESS $A8_B3_COIN --keyring-backend $KEYRING_BACKEND"
printf '%s\n' "$A8_B3_COMMAND" > "$A8_PROVISION_DIR/b3-command.txt"
echo "Adding the A8 B3 foreign native denom account"
$APP_NAME genesis add-genesis-account "$A8_B3_FOREIGN_ADDRESS" "$A8_B3_COIN" --keyring-backend $KEYRING_BACKEND
cp "$A8_GENESIS_FILE" "$A8_PROVISION_DIR/genesis-after-b3.json"
# ---------------------------------------------------------------------------

# Get the warm key address for ML operations
WARM_KEY_ADDRESS=$($APP_NAME keys show "$KEY_NAME_WARM" --address --keyring-backend $KEYRING_BACKEND --keyring-dir "$STATE_DIR")

# Use PUBLIC_URL if set, otherwise provide a reasonable default
URL_VALUE="${PUBLIC_URL:-http://localhost:9000}"

$APP_NAME genesis gentx "$KEY_NAME" "1$MILLION_BASE" --chain-id "$CHAIN_ID" \
  --moniker "mynode" \
  --url "$URL_VALUE" \
  --ml-operational-address "$WARM_KEY_ADDRESS" \
  || {
  echo "Failed to create gentx"
  tail -f /dev/null
}
output=$($APP_NAME genesis collect-gentxs 2>&1)
echo "$output" | filter_cw20_code

# Patch genesis with genparticipant transactions
echo "Patching genesis with genparticipant transactions"
output=$($APP_NAME genesis patch-genesis 2>&1)
echo "$output" | filter_cw20_code

# tgbot
TG_ACC=gonka1va4hlpg929n6hhg4wc8hl0g9yp4nheqxm6k9wr

if [ "$INIT_TGBOT" = "true" ]; then
  echo "Adding the tgbot account"
  $APP_NAME genesis add-genesis-account $TG_ACC "100$MILLION_NATIVE" --keyring-backend $KEYRING_BACKEND
fi

modify_genesis_file 'genesis_overrides.json'
modify_genesis_file "$HOME/.inference/genesis_overrides.json"
echo "Genesis file created"
echo "Setting up overrides for config.toml"
 # Process CONFIG_ environment variables
 for var in $(env | grep '^CONFIG_'); do
    # Extract key and value
    key=${var%%=*}
    value=${var#*=}

    # Remove CONFIG_ prefix and transform __ to .
    config_key=${key#CONFIG_}
    config_key=$(printf '%s\n' "$config_key" | sed 's/__/./g')

    echo "Setting config: $config_key = $value"
    $APP_NAME config set config "$config_key" "$value" --skip-validate
 done
# Check and apply config overrides if present
if [ -f "config_override.toml" ]; then
    echo "Applying config overrides from config_override.toml"
    $APP_NAME patch-toml "$STATE_DIR/config/config.toml" config_override.toml
fi

update_configs

# --- A8 final genesis evidence and validation (difference 2 and 3) ----------
cp "$A8_GENESIS_FILE" "$A8_PROVISION_DIR/genesis-final.json"
if ! $APP_NAME genesis validate "$A8_GENESIS_FILE" > "$A8_PROVISION_DIR/genesis-validate.log" 2>&1; then
  cat "$A8_PROVISION_DIR/genesis-validate.log"
  echo "A8 provisioner: final genesis failed '$APP_NAME genesis validate'" >&2
  exit 1
fi
cat "$A8_PROVISION_DIR/genesis-validate.log"
(
  cd "$A8_PROVISION_DIR"
  sha256sum genesis-before-b3.json > genesis-before-b3.json.sha256
  sha256sum genesis-after-b3.json > genesis-after-b3.json.sha256
  sha256sum genesis-final.json > genesis-final.json.sha256
)
A8_SHA_BEFORE=$(cut -d' ' -f1 "$A8_PROVISION_DIR/genesis-before-b3.json.sha256")
A8_SHA_AFTER=$(cut -d' ' -f1 "$A8_PROVISION_DIR/genesis-after-b3.json.sha256")
A8_SHA_FINAL=$(cut -d' ' -f1 "$A8_PROVISION_DIR/genesis-final.json.sha256")
printf '{\n  "schema": "a8.b3-provision/1",\n  "derived_from_sha256": "%s",\n  "command": "%s",\n  "address": "%s",\n  "denom": "%s",\n  "amount": "%s",\n  "genesis_validate": "passed",\n  "sha256": {\n    "genesis-before-b3.json": "%s",\n    "genesis-after-b3.json": "%s",\n    "genesis-final.json": "%s"\n  }\n}\n' \
  "$A8_PROVISIONER_DERIVED_FROM_SHA256" "$A8_B3_COMMAND" "$A8_B3_FOREIGN_ADDRESS" \
  "$A8_B3_FOREIGN_DENOM" "$A8_B3_FOREIGN_AMOUNT" \
  "$A8_SHA_BEFORE" "$A8_SHA_AFTER" "$A8_SHA_FINAL" \
  > "$A8_PROVISION_DIR/b3-provision.json"
# ---------------------------------------------------------------------------

echo "Init for cosmovisor"
cosmovisor init /usr/bin/inferenced || {
  echo "Cosmovisor failed, idling the container..."
  tail -f /dev/null
}

echo "Starting cosmovisor and the chain"
#cosmovisor run start || {
#  echo "Cosmovisor failed, idling the container..."
#  tail -f /dev/null
#}

# gRPC bind (0.0.0.0:9090) comes from app_overrides.toml via update_configs when REST_API_ACTIVE=true.

cosmovisor run start &
COSMOVISOR_PID=$!
sleep 20 # wait for the first block

# import private key for tgbot and sign tx to make tgbot public key registered n the network
if [ "$INIT_TGBOT" = "true" ]; then
    echo "Initializing tgbot account..."

    # The upstream script enables xtrace globally. Disable it before even
    # testing the secret variable, because xtrace prints expanded arguments.
    set +x
    if [ -z "$TGBOT_PRIVATE_KEY_PASS" ]; then
        echo "Error: TGBOT_PRIVATE_KEY_PASS is empty. Aborting initialization."
        exit 1
    fi

    printf '%s\n' "$TGBOT_PRIVATE_KEY_PASS" | inferenced keys import tgbot tgbot_private_key.json
    set -x

    inferenced tx bank send $TG_ACC $TG_ACC 100nicoin --from tgbot --yes
    echo "✅ tgbot account successfully initialized!"
else
    echo "INIT_TGBOT is not set to true. Skipping tgbot initialization."
fi

wait $COSMOVISOR_PID

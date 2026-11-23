# Part 1: Delegation, access checks, and revocation

The files under [`logs/`](logs/) record a single-Auth SST/IoTAuth experiment with `Node0`–`Node6` and `ResourceA`–`ResourceD`. The `t_<k>` prefix identifies the workflow stage. `Auth_logs/` records Auth policy operations, and `Node_logs/` records the client-side test results and latency.

The archived node output matches the historical Node.js runners in `iotauth/entity/node/example_entities/`. Both approaches use `autoAccess.js` for the 12 access checks. The proposed approach uses `autoDelegate1.js`–`autoDelegate4.js` to grant access and `autoRevoke1.js`–`autoRevoke2.js` to revoke access explicitly. The baseline adds policies through the Auth console and removes them manually.

The supplied `baseline.py` and archived `scenario1_*.py` and `access_checking.py` exercise related operations through `client.js` or `user.js`. Their output format differs from the saved Node.js logs. In particular, `scenario1_delegate2.py` uses one-day grants, while the saved `t_2` uses five and six minutes.

## Set up two local runs

Use Git, Bash or Zsh, Node.js with npm, a JDK, Maven, Make, OpenSSL 3, and the `sqlite3` CLI. The historical graph and stage-specific runners are available together at IoTAuth commit `5790ee98bd03e1094e8db1b8950d94404e3059ba`. This is the replay revision selected by matching the scripts and graph to the logs. The exact revision of the original Auth binary is unverified.

From the root of `multi-delegation`, extract that revision into two temporary copies. Each copy has its own credentials and Auth database. If you have a fresh checkout, initialize the submodule first with `git submodule update --init iotauth`.

```bash
export PART1_RUNS="$(mktemp -d "${TMPDIR:-/tmp}/sst-part1.XXXXXX")"
revision=5790ee98bd03e1094e8db1b8950d94404e3059ba

git -C iotauth cat-file -e "$revision^{commit}"
for approach in baseline proposed; do
  mkdir -p "$PART1_RUNS/$approach" "$PART1_RUNS/logs/$approach/"{Auth_logs,Node_logs}
  git -C iotauth archive "$revision" | tar -x -C "$PART1_RUNS/$approach"
done
printf '%s\n' "$PART1_RUNS"
```

If Git cannot find the commit, fetch IoTAuth history with `git -C iotauth fetch origin`. For a shallow submodule, use `git -C iotauth fetch --unshallow origin`.

On macOS, the system LibreSSL also works for credential generation. If `openssl version` reports OpenSSL 4, put OpenSSL 3 or the system LibreSSL first on `PATH` before generating credentials. The historical CA script can fail to read `CAKey.pem` with OpenSSL 4. For example, with Homebrew OpenSSL 3 installed, run `export PATH="$(brew --prefix openssl@3)/bin:$PATH"`.

```bash
for approach in baseline proposed; do
  (
    cd "$PART1_RUNS/$approach/examples" &&
      ./generateAll.sh -g configs/multi_agent.graph &&
    cd ../auth/auth-server &&
      make
  ) || break
done
```

Choose a test password at the generator prompt and use the same password when starting that copy of Auth. The graph generates Auth 101, the seven node configs, the four resource configs, and initial access from `Node0` to all four resources.

## Start Auth and the resource servers

Run the two approaches one at a time. Apply the initial [baseline policies](#baseline) before starting the baseline Auth server. They share Auth TCP port `21900` and resource ports `21100`, `21200`, `21300`, and `21400`. In each terminal, export the `PART1_RUNS` path printed during setup and set `approach=proposed` or `approach=baseline` for the run.

In the Auth terminal, start Auth and enter the password from generation. Wait for `Auth server information. Auth ID: 101` and the server startup messages.

```bash
cd "$PART1_RUNS/$approach/auth/auth-server"
java -jar target/auth-server-jar-with-dependencies.jar \
  -p ../properties/exampleAuth101.properties \
  2> >(tee -a "$PART1_RUNS/logs/$approach/Auth_logs/auth101.txt" >&2)
```

In a second terminal, start the four resource servers and leave them running through the access checks.

```bash
cd "$PART1_RUNS/$approach/entity/node/example_entities"
resource_pids=()
for resource in A B C D; do
  node server.js "configs/net1/resource$resource.config" \
    > "$PART1_RUNS/logs/$approach/Node_logs/resource$resource.txt" 2>&1 &
  resource_pids+=("$!")
done
```

Run the client commands below from the same `entity/node/example_entities/` directory in a third terminal. To save a stage log, append `2>&1 | tee "$PART1_RUNS/logs/$approach/Node_logs/t_<k>.txt"` to its command, replacing `<k>` with the stage number. Check the script's `SUMMARY` for the result. Auth output is captured across stages in `Auth_logs/auth101.txt`.

## Proposed approach

Run the following stages in order. Start `t_5` promptly after the grants so the short `t_2` policies are still active.

| Stage | Command or action | Operation |
| --- | --- | --- |
| `t_1` | `node autoDelegate1.js` | `Node0` grants `Node1` access to `ResourceA` and `ResourceB` for one day. |
| `t_2` | `node autoDelegate2.js` | `Node0` grants `Node2` access to `ResourceC` for five minutes and `ResourceD` for six minutes. |
| `t_3` | `node autoDelegate3.js` | `Node1` grants `Node3` access to `ResourceA` and `Node4` access to `ResourceB`. |
| `t_4` | `node autoDelegate4.js` | `Node2` grants `Node5` access to `ResourceC` and `Node6` access to `ResourceD`. The child policies inherit the earlier expiration of their parent policies. |
| `t_5` | `node autoAccess.js` | All 12 access checks succeed. |
| `t_6` | `node autoRevoke1.js` | `Node0` revokes `Node1`'s `ResourceA` grant. Auth also removes `Node3`'s derived policy. |
| `t_7` | `node autoRevoke2.js` | `Node0` revokes `Node1`'s `ResourceB` grant. Auth also removes `Node4`'s derived policy. |
| `t_8`–`t_9` | Wait for Auth's expiration cleanup | The `ResourceC` and `ResourceD` policies for `Node2`, `Node5`, and `Node6` expire. Wait for Auth to report removal of all four policies. Cleanup runs every two minutes, so allow up to about eight minutes from `t_2`. |
| `t_10` | `node autoAccess.js` | Only `Node0`'s four access checks succeed. |

The saved [`t_8`–`t_9` Auth log](logs/proposed_approach/Auth_logs/t_8_9_auth_prop_revoke3-4.txt) captures expiration cleanup for policy IDs `36`, `37`, `40`, and `41`. The historical graph defines explicit revoke privileges for the two `Node1` grants. The `autoRevoke3.js` and `autoRevoke4.js` files in that revision issue explicit requests for the `Node2` grants and are outside the recorded sequence.

## Baseline

1. The original Auth logs show eight static policies entered with `add cp` at `t_1`–`t_4`. On the replay revision, the console assigns the same ID to successive policies. Before starting baseline Auth, insert the same eight policies into its fresh database with unique IDs. The first column in `VALUES` identifies the recorded stage.

   ```bash
   sqlite3 -bail "$PART1_RUNS/baseline/auth/databases/auth101/auth.db" <<'SQL'
   WITH grants(stage, requester, resource) AS (
     VALUES (1, 'Node1', 'ResourceA'), (1, 'Node1', 'ResourceB'),
            (2, 'Node2', 'ResourceC'), (2, 'Node2', 'ResourceD'),
            (3, 'Node3', 'ResourceA'), (3, 'Node4', 'ResourceB'),
            (4, 'Node5', 'ResourceC'), (4, 'Node6', 'ResourceD')
   )
   INSERT INTO communication_policy
     (ID, RequestingGroup, TargetType, Target, MaxNumSessionKeyOwners,
      SessionCryptoSpec, AbsoluteValidity, RelativeValidity, Expiration, IsDelegated)
   SELECT (SELECT MAX(ID) FROM communication_policy)
            + ROW_NUMBER() OVER (ORDER BY stage, requester, resource),
          requester, 'Group', resource, 2,
          'AES-128-CBC:SHA256', '1*day', '1*hour', 9223372036854775807, 0
   FROM grants;
   SELECT changes();
   SQL
   ```

   Expect `8`. Start baseline Auth and the resource servers as described above.

2. At `t_5`, run `node autoAccess.js`. Expect 12 successes out of 12.
3. At `t_6`–`t_9`, stop the baseline Auth process. In the temporary baseline database at `$PART1_RUNS/baseline/auth/databases/auth101/auth.db`, remove the four direct policies added at `t_1` and `t_2` from the `communication_policy` table. The original run used SQLPro Studio. The same edit can be made with SQLite:

   ```bash
   sqlite3 -bail "$PART1_RUNS/baseline/auth/databases/auth101/auth.db" <<'SQL'
   DELETE FROM communication_policy
   WHERE (RequestingGroup = 'Node1' AND Target IN ('ResourceA', 'ResourceB'))
      OR (RequestingGroup = 'Node2' AND Target IN ('ResourceC', 'ResourceD'));
   SELECT changes();
   SQL
   ```

   Expect `4`. Restart baseline Auth with the same database and password, keeping the four resource servers running.

4. At `t_10`, run `node autoAccess.js` again. Expect 8 successes out of 12. `Node1` and `Node2` lose access to their four targets, while the manual policies for `Node3`–`Node6` remain in effect.

The recorded `t_5` and `t_10` summaries are in [baseline `Node_logs/`](logs/baseline/Node_logs/) and [proposed `Node_logs/`](logs/proposed_approach/Node_logs/). Before switching approaches, stop Auth with Ctrl+C and run `kill "${resource_pids[@]}"` in the resource server terminal.

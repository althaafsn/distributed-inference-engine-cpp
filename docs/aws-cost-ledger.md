# AWS benchmark cost ledger

Budget ceiling: USD $10 for this project. Figures below are estimates for the resources created by these benchmark attempts, not a verified AWS bill or an enforceable spending limit. Costs for other account activity are excluded.

Rates checked on 2026-11-02: AWS Pricing API returned $0.085 per hour for Linux on-demand `c6i.large` and $0.08 per GB-month for gp3 in `us-east-1`. AWS lists $0.005 per hour for an in-use public IPv4 address. Each attempt used one instance, one 12 GB gp3 root volume and one public IPv4 address. See [AWS public IPv4 pricing](https://aws.amazon.com/vpc/pricing/).

| Attempt | Instance runtime used for estimate | Compute | IPv4 | 12 GB gp3 | Estimated subtotal |
| --- | ---: | ---: | ---: | ---: | ---: |
| First: package conflict | 69 seconds | $0.001629 | $0.000096 | $0.000026 | $0.001751 |
| Second: successful baseline | 155 seconds | $0.003660 | $0.000215 | $0.000057 | $0.003932 |
| Third: instrumented timing run | 155 seconds | $0.003660 | $0.000215 | $0.000057 | $0.003932 |
| Fourth: matched baseline | 156 seconds | $0.003683 | $0.000217 | $0.000058 | $0.003958 |
| Fifth: changed source, 2 ORT threads | 148 seconds | $0.003494 | $0.000206 | $0.000055 | $0.003755 |
| Sixth: changed source repeat | 138 seconds | $0.003258 | $0.000192 | $0.000051 | $0.003501 |
| Seventh: matched baseline repeat | 156 seconds | $0.003683 | $0.000217 | $0.000058 | $0.003958 |
| Eighth: TCP_NODELAY, thread-tuned source | 129 seconds | $0.003046 | $0.000179 | $0.000048 | $0.003273 |
| Ninth: TCP_NODELAY repeat | 136 seconds | $0.003211 | $0.000189 | $0.000050 | $0.003450 |
| Tenth: 10 ms batch deadline | 126 seconds | $0.002975 | $0.000175 | $0.000047 | $0.003197 |
| Eleventh: 10 ms batch deadline repeat | 127 seconds | $0.002999 | $0.000176 | $0.000047 | $0.003222 |
| Twelfth: gateway response passthrough | 127 seconds | $0.002999 | $0.000176 | $0.000047 | $0.003222 |
| Thirteenth: gateway response passthrough repeat | 140 seconds | $0.003306 | $0.000194 | $0.000052 | $0.003552 |
| Total | 1762 seconds | $0.041603 | $0.002447 | $0.000653 | **$0.044703** |

## Multi-host campaign

The multi-host campaign ran 34 tagged topologies. It tested 1, 2, 4, and 8 workers, two routing policies, five models, two transports, sustained holds, and worker failure. The first five rows are failed smoke attempts that still incurred instance time. The two failover timeout rows are real retries, not free attempts.

| Run | Purpose | Seconds | Source | c6i.large | c6i.xlarge | Estimated cost |
| --- | --- | ---: | --- | ---: | ---: | ---: |
| `infer-01625ee5bd706ff0` | 8-worker hold, 69 requests/s | 648 | log | 9 | 1 | $0.179700 |
| `infer-146e5898561d4f8c` | Failover attempt: marker timeout | 1145 | log | 9 | 1 | $0.317525 |
| `infer-22d7935dd62ad193` | Binary transport comparison | 304 | log | 3 | 1 | $0.038028 |
| `infer-27376600dcf2839f` | Smoke 1: launch failure | 284 | log | 2 | 1 | $0.028321 |
| `infer-27787d94d40229e6` | 8-worker failover and consistency | 415 | log | 9 | 1 | $0.115086 |
| `infer-38fce5baffb45b73` | ShuffleNet screen: fixed | 370 | log | 3 | 1 | $0.046284 |
| `infer-3b483e9f0e17133e` | 4-worker least-loaded capacity | 684 | log | 5 | 1 | $0.120270 |
| `infer-4cb9d4d6b318efbf` | Smoke 3: launch failure | 167 | log | 2 | 1 | $0.016654 |
| `infer-4fb41cf43bfe9fc7` | 2-worker full-SLO hold, 12 requests/s | 495 | exact | 3 | 1 | $0.061921 |
| `infer-5548480f3196fc8e` | Smoke 2: launch failure | 282 | log | 2 | 1 | $0.028122 |
| `infer-5fd1ee38b8821e83` | 2-worker accuracy; cache-contaminated levels | 298 | log | 3 | 1 | $0.037278 |
| `infer-72587b02bbc2e3a1` | 8-worker CPU-fail hold, 60 requests/s | 593 | exact | 9 | 1 | $0.164448 |
| `infer-77f5980e5018e459` | 4-worker full-SLO hold, 24 requests/s | 524 | exact | 5 | 1 | $0.092137 |
| `infer-7bcee360549dabf9` | 2-worker least-loaded capacity | 534 | log | 3 | 1 | $0.066799 |
| `infer-7d2ff399260c7b05` | Smoke 4: launch failure | 178 | log | 2 | 1 | $0.017751 |
| `infer-83b8dbd4d084cdc0` | 8-worker hash capacity, final code | 838 | log | 9 | 1 | $0.232390 |
| `infer-89263c6af926b86d` | JSON transport comparison | 338 | log | 3 | 1 | $0.042281 |
| `infer-95177361d974809a` | ShuffleNet screen: batch bug | 373 | log | 3 | 1 | $0.046660 |
| `infer-995c0ed344fd4b27` | Failover attempt: marker timeout | 358 | log | 9 | 1 | $0.099279 |
| `infer-9b33aaad4138a3ab` | 8-worker full-SLO hold, 45 requests/s | 593 | exact | 9 | 1 | $0.164448 |
| `infer-a709308362635b90` | 8-worker least-loaded capacity | 825 | log | 9 | 1 | $0.228785 |
| `infer-abece6d2475cf3b2` | MobileNetV2 screen | 384 | log | 3 | 1 | $0.048036 |
| `infer-af28a46c98b37890` | ResNet18 screen | 402 | log | 3 | 1 | $0.050287 |
| `infer-c0e784095ecbb4ec` | 1-worker full-SLO hold, 7 requests/s | 471 | exact | 2 | 1 | $0.046969 |
| `infer-c3758d5650859167` | 4-worker hash capacity | 521 | log | 5 | 1 | $0.091609 |
| `infer-caddecbab4a71b12` | 8-worker hold, 86 requests/s mixed | 628 | log | 9 | 1 | $0.174154 |
| `infer-ce1676400f41cfb3` | Smoke 5: 1-worker pass | 202 | log | 2 | 1 | $0.020144 |
| `infer-ce2a249418f67507` | 8-worker hash capacity, pre-routing | 598 | log | 9 | 1 | $0.165834 |
| `infer-d0b6b9aeb075e99d` | 2-worker hash capacity | 450 | log | 3 | 1 | $0.056292 |
| `infer-d41d6985fa110f4f` | ResNet34 screen | 425 | log | 3 | 1 | $0.053164 |
| `infer-e81b49f6d3f17765` | 1-worker capacity, 500 requests | 458 | log | 2 | 1 | $0.045673 |
| `infer-faec7003a94741a3` | 8-worker CPU-boundary hold, 50 requests/s | 591 | exact | 9 | 1 | $0.163893 |
| `infer-fd3725f22b42726b` | 8-worker hold, 84 requests/s unique | 630 | log | 9 | 1 | $0.174708 |
| `infer-ff084d65cba740e2` | 1-worker cold pilot, 200 requests | 325 | log | 2 | 1 | $0.032410 |
| **Total** |  |  |  |  |  | **$3.267337** |

## Admission validation (2026-11-04)

One `c6i.large` gateway, two `c6i.large` workers, and one `c6i.xlarge` load generator ran for about 17 minutes 40 seconds. Runtime is the interval from EC2 `LaunchTime` to the recorded termination at `2026-11-04 06:08:26 GMT`. The controller verified cleanup; `describe-volumes` and `describe-security-groups` returned not found for every recorded volume and group.

| Resource | Type | Seconds | Compute | IPv4 | 12 GB gp3 | Estimated subtotal |
| --- | --- | ---: | ---: | ---: | ---: | ---: |
| Gateway | c6i.large | 1064 | $0.025122 | $0.001478 | $0.000394 | $0.026994 |
| Worker 1 | c6i.large | 1062 | $0.025075 | $0.001475 | $0.000393 | $0.026943 |
| Worker 2 | c6i.large | 1059 | $0.025004 | $0.001471 | $0.000392 | $0.026867 |
| Load generator | c6i.xlarge | 1057 | $0.049914 | $0.001468 | $0.000391 | $0.051773 |
| **Total** | | **4,242** | **$0.125115** | **$0.005892** | **$0.001571** | **$0.132578** |

## Failed ALB HA attempt (2026-11-04)

Five instances ran for 170–178 seconds. The run failed at `CreateTargetGroup` because the deploy user lacks ELB target-group permissions. No ALB or target group was created. All instances were terminated; volumes and security groups were removed.

| Resource | Type | Seconds | Compute | IPv4 | 12 GB gp3 | Estimated subtotal |
| --- | --- | ---: | ---: | ---: | ---: | ---: |
| Gateway 1 | c6i.large | 178 | $0.004203 | $0.000247 | $0.000066 | $0.004516 |
| Gateway 2 | c6i.large | 176 | $0.004156 | $0.000244 | $0.000065 | $0.004465 |
| Worker 1 | c6i.large | 174 | $0.004108 | $0.000242 | $0.000064 | $0.004414 |
| Worker 2 | c6i.large | 172 | $0.004061 | $0.000239 | $0.000064 | $0.004364 |
| Load generator | c6i.xlarge | 170 | $0.008028 | $0.000236 | $0.000063 | $0.008327 |
| **Total** | | **870** | **$0.024556** | **$0.001208** | **$0.000322** | **$0.026086** |

Grand total: **$0.044703 + $3.267337 + $0.132578 + $0.026086 = $3.470704**. The remaining budget is about **$6.53**.

For the multi-host table, `exact` means the interval from EC2 `LaunchTime` to the recorded termination time. `log` means the console-log end time minus the recorded run start time, because AWS had already aged the instance records out of `describe-instances`. The `log` method includes provisioning and cleanup, so it is conservative. The two failover timeout rows include the full wait time before cleanup.

Method for the 13 single-host rows: runtime is the interval from EC2 `LaunchTime` to the recorded user-initiated termination time. Compute = seconds / 3600 × $0.085; IPv4 = seconds / 3600 × $0.005; storage = 12 × $0.08 × seconds / (30 × 24 × 3600). Each instance ran more than the minimum one-minute billing period. Storage and IP lifetimes are approximated by instance runtime; actual provision and deletion can differ. Data transfer, tax, credits, discounts and billing adjustments are not included. AWS billing may arrive later; consult the bill for actual charges.

The controller verified termination after each run. Direct AWS checks confirmed that the recorded instance was terminated and its recorded volume and security group did not exist after each attempt. The first run was `infer-85e0e6c87756c37a`; the second was `infer-b3ee717ce6dc11fe`; the third was `infer-8bb17685b18e63a4`; the fourth was `infer-d4522a282d0dcc42`; the fifth was `infer-1f39a2ac8519f743`; the sixth was `infer-0601374edef6f33d`; the seventh was `infer-ca8db4050033ae08`; the eighth was `infer-ff9ac124eb8e79ea`; the ninth was `infer-2063d463870454ba`; the tenth was `infer-f6dfdf8dd788978c`; the eleventh was `infer-d1070c813d774208`; the twelfth was `infer-a5f5bc6de001b9ae`; the thirteenth was `infer-f8f48cbffd6ffd98`. No additional AWS resources were launched for the local tests. The separate admission validation above ran on 2026-11-04. Cost Explorer reported $0.000000 for 2026-11-01 through 2026-11-03 at the time of writing; billing data can be delayed, so the ledger estimates above remain the conservative record.

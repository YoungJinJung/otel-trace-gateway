# OTel Trace Gateway

[English](README.md) | 한국어

Datadog 트레이서 프로토콜을 그대로 수신하는 중앙 [OpenTelemetry Collector](https://opentelemetry.io/docs/collector/) 배포본입니다. 계측된 앱은 `DD_TRACE_AGENT_URL`만 이 게이트웨이로 바꾸면 되고, 재계측은 필요 없습니다. 모든 span을 하나의 APM 호스트 이름으로 고정해, Datadog의 호스트 단위 APM 과금을 워크로드의 pod·노드 확산과 분리합니다.

환경 고유의 이름·경로는 `<...>` 플레이스홀더로 두었습니다.

## 1. 문제의 구조

### APM 호스트 과금 방식

Datadog APM은 **트레이스를 제출하는 호스트 이름 단위**로 과금합니다. High Watermark Plan에서는 시간마다 동시 호스트 수를 기록하고, 월말에 내림차순 정렬해 **9번째로 높은 값**으로 청구합니다. 즉 **월간 상위 8시간은 무료**입니다.

p99가 아니라는 점이 중요합니다 — 드문 스파이크는 비용이 0이고, 반복되는 스파이크는 전액을 만듭니다.

> Fargate는 다른 모델입니다: 월 **평균** 동시 태스크 수 기준 정액(watermark 아님). 버스티한 단명 워크로드에는 유리할 수 있습니다.

### 왜 단명 워크로드에서 터지는가

표준 노드 로컬 수집에서는 pod가 자기 노드의 에이전트로 보고합니다. 따라서 **pod가 한 번이라도 내려앉은 모든 노드가 그 시간의 APM 호스트**가 됩니다.

태스크당 pod 하나를 오토스케일 노드풀에서 돌리는 워크로드는 이 구조에서 최악입니다 — 호스트 수가 pod·노드 확산을 그대로 따라가고, 사용량이 늘면 비례해 올라갑니다.

### 해결 원리

모든 span을 **하나의 호스트 이름**으로 고정해 제출하면, 호스트 수가 워크로드 확산과 분리됩니다. 중앙 게이트웨이가 그 역할을 합니다.

## 2. Datadog Agent로는 안 되는 이유

게이트웨이를 Datadog Agent로 세우는 접근이 먼저 떠오르지만, **동작하지 않습니다.**

Datadog Agent의 태거는 **자기 노드의 kubelet에서만 pod를 읽습니다.** 다른 노드에서 실행 중인 pod의 메타데이터를 해석할 경로가 없고, 설정으로 우회되지 않습니다. Cluster Agent도 "로컬에서 수집된" 것을 보강할 뿐입니다.

결과적으로 `pod_name` `container_id` `kube_namespace` 같은 에이전트 태그가 전부 사라집니다. `DD_TAGS`로 되살릴 수는 있지만 **span 속성(`@pod_name`)으로 들어와** 기존 쿼리·대시보드와 호환되지 않습니다.

### OTel Collector는 되는 이유

`k8sattributes` 프로세서가 노드 로컬 태거가 아니라 **Kubernetes API 서버를 직접 조회**합니다. pod가 어느 노드에 있든 정체성이 해석되고, 그 리소스 속성이 **네이티브 Datadog 태그로 매핑**됩니다.

`datadogreceiver`가 Datadog 트레이서 프로토콜(v0.3/0.4/0.5)을 그대로 수신하므로 **애플리케이션은 재계측이 필요 없습니다.** `DD_TRACE_AGENT_URL`만 바꾸면 됩니다.

## 3. 아키텍처

```
app pods (기존 트레이서 그대로)
      |  DD_TRACE_AGENT_URL -> <gateway-service>:8126
      v
+---------------- OTel Collector (Deployment, 중앙) ----------------+
|  datadogreceiver          Datadog 트레이서 프로토콜 수신           |
|  filter/ignore_resources  health/liveness 루트 span 제거          |
|  transform/promote        k8s.container.name -> 리소스 속성       |
|  k8sattributes            K8s API 조회: pod/컨테이너/노드레이블    |
|  transform/identity       datadog.host.name 고정 -> APM 호스트 1  |
|                           kube_ownerref_* 합성                    |
|  transform/opname         operation.name 복원                     |
|  datadog/connector        APM trace metrics 생성                  |
|  datadog exporter         -> Datadog                              |
+---------------------------------------------------------------------+
```

## 4. 보존되는 것과 잃는 것

**네이티브 Datadog 태그로 복원**
`pod_name` `kube_namespace` `kube_deployment` `kube_replica_set` `container_id` `kube_container_name` `image_name` `image_tag` `zone` `region` + 클러스터 공통 태그(`host_metadata.tags`로 주입)

**span 속성(`@` 접두사)으로 확보**
`@host.type` `@karpenter_nodepool` `@k8s.node.name` `@k8s.pod.uid` `@kube_ownerref_kind` `@kube_ownerref_name`

**영구 손실 — 이 차트의 미비가 아니라 구조적입니다**
`dd_resource_key`(APM→EC2 인스턴스 피벗), `security-group`, `iam_profile`, 런치 템플릿 태그, `aws_account`, `pod_phase`, `kube_qos`

호스트 레벨 태그는 **조회 시점에 호스트 이름으로 조인**되는데, 호스트 이름을 고정하는 것이 곧 절감의 메커니즘입니다. **호스트 수 상한과 호스트 엔티티 태그는 동시에 가질 수 없습니다.** 트레이드오프이지, 빠진 기능이 아닙니다 — 도입 전에 이 손실을 수용할 수 있는지부터 정하세요.

## 5. Collector 설정 — 빠뜨리면 안 되는 4가지

각각 빠뜨리면 다르게 깨지고, 일부는 **조용히** 깨집니다.

### 5.1 `transform/opname` — operation name 복원

```yaml
transform/opname:
  trace_statements:
    - set(span.attributes["operation.name"], span.name) where span.attributes["operation.name"] == nil
```

**빠뜨리면**: exporter가 span kind로 operation name을 재계산합니다(`express.request` → `Internal`, 또는 v1 로직에서 `<scope>.<kind>`). **`trace.<op>.hits` 지표 이름까지 따라 바뀌어** 그 지표로 걸어둔 모니터·SLO·대시보드가 전부 깨집니다.

`operation.name` 속성이 두 명명 모드보다 **최우선**합니다.

### 5.2 `transform/promote` — 컨테이너 계층 조건

```yaml
transform/promote:
  trace_statements:
    - set(resource.attributes["k8s.container.name"], span.attributes["k8s.container.name"]) where span.attributes["k8s.container.name"] != nil
```

**빠뜨리면**: `container_id` `image_name` `image_tag`가 비어 있습니다.

`k8sattributes`는 컨테이너 태그를 붙이려면 `container.id` 또는 `k8s.container.name`을 **리소스 속성**으로 요구하는데, 워크로드가 `DD_TAGS`로 실어 보낸 값은 **span 속성**으로 도착합니다. 그래서 `k8sattributes`보다 **먼저** 승격시켜야 합니다.

> pod IP는 pod를 식별할 뿐 **그 안의 어느 컨테이너가 span을 냈는지는 말해주지 않습니다.** 그래서 컨테이너 이름만은 워크로드가 알려줘야 합니다.

### 5.3 `extract.labels` + `from: node` — 노드 정보 회수

호스트 엔티티 태그는 잃지만, **상당수는 노드 레이블로 살아 있습니다.**

```yaml
k8sattributes:
  extract:
    labels:
      - { tag_name: host.type,               key: node.kubernetes.io/instance-type, from: node }
      - { tag_name: cloud.availability_zone, key: topology.kubernetes.io/zone,      from: node }
      - { tag_name: cloud.region,            key: topology.kubernetes.io/region,    from: node }
```

`cloud.availability_zone` / `cloud.region`은 **네이티브 `zone` / `region`으로 매핑**됩니다. 오토스케일러가 붙이는 레이블(노드풀·노드그룹 등)도 같은 방식으로 가져올 수 있습니다.

### 5.4 `datadog/connector` — APM 지표

```yaml
connectors:
  datadog/connector:
    traces:
      compute_stats_by_span_kind: true
      peer_tags_aggregation: true

service:
  pipelines:
    traces:
      exporters: [datadog/connector, datadog]
    metrics:
      receivers: [datadog/connector]
      exporters: [datadog]
```

**빠뜨리면 APM trace metrics가 아예 생성되지 않습니다.** Collector가 기동 시 경고를 남기지만 놓치기 쉽습니다.

### 5.5 리소스 필터의 `IsRootSpan()` 가드

노드 에이전트의 `DD_APM_IGNORE_RESOURCES`와 보조를 맞추려면 같은 패턴을 Collector에도 넣어야 합니다. 이때 **`IsRootSpan()`이 필수입니다.**

```yaml
filter/ignore_resources:
  error_mode: ignore
  traces:
    span:
      - IsRootSpan() and IsMatch(span.attributes["dd.span.Resource"], "^GET$")
```

노드 에이전트는 **루트 span**의 resource만 보고 그 trace를 버리지만, filter 프로세서는 **모든 span**에 걸립니다. 가드가 없으면 `^GET$` 같은 패턴이 redis `GET`·cache `GET`·outbound `GET` 같은 **정상적인 자식 span까지** 지웁니다. 한 서비스에서만 하루 수십만 건 규모가 될 수 있습니다.

`datadogreceiver`는 Datadog resource 이름을 **`dd.span.Resource`** 속성에 넣습니다.

> **완전히 동일하지는 않습니다.** 노드 에이전트는 trace 전체를 버리고, 이쪽은 매칭된 루트만 버리고 자식은 남깁니다. trace 단위로 맞추려면 `tail_sampling` + replica 간 load-balancing이 필요해 범위가 크게 늘어납니다.

### 5.6 호스트 고정과 공통 태그

```yaml
transform/identity:
  trace_statements:
    - set(resource.attributes["datadog.host.name"], "<gateway-hostname>")

exporters:
  datadog:
    hostname: "<gateway-hostname>"
    host_metadata:
      enabled: true              # 켜야 tags가 네이티브 호스트 태그가 됨
      hostname_source: config_or_system
      tags: ["<key>:<value>", ...]
```

`datadog.host.name` 고정이 **호스트 수를 1로 묶는 장치**입니다. `host_metadata.tags`는 노드 EC2 태그가 사라진 자리를 메웁니다 — **클러스터 전체가 동일한 값만** 넣으세요.

## 6. Helm 차트 구성

| 파일 | 역할 |
|---|---|
| `Chart.yaml` | `appVersion`을 Collector 버전으로 |
| `values.yaml` | 환경 무관 기본값 |
| `templates/configmap.yaml` | **Collector 설정 전체** |
| `templates/deployment.yaml` | `checksum/config` 애노테이션 → 설정 변경 시 자동 롤 |
| `templates/rbac.yaml` | `k8sattributes`용 SA + ClusterRole |
| `templates/externalsecret.yaml` | API 키를 시크릿 저장소에서 |
| `templates/service.yaml` | 워크로드가 가리키는 DNS 이름 |
| `templates/hpa.yaml`, `pdb.yaml` | 오토스케일 / 중단 예산 |

이 레포는 [External Secrets Operator](https://external-secrets.io/)로 `ClusterSecretStore`에서 Datadog API 키를 끌어옵니다. ESO를 안 쓴다면 `templates/externalsecret.yaml`을 일반 `Secret`으로 바꾸면 됩니다.

### 필수 RBAC

```yaml
rules:
  - apiGroups: [""]
    resources: ["pods", "namespaces", "nodes"]
    verbs: ["get", "list", "watch"]
  - apiGroups: ["apps"]
    resources: ["replicasets"]
    verbs: ["get", "list", "watch"]
```

이 읽기 권한이 **다른 노드의 pod를 태깅할 수 있는 근거 전체**입니다.

### 배치와 스케줄링

- **자체 배포 단위로 분리하세요.** 모니터링 스택 안에 두면 게이트웨이 장애가 모니터링 전체를 끌고 갑니다.
- **오토스케일 노드가 아니라 고정 노드에 배치하세요.** 소멸·재생성되는 노드에 두면 게이트웨이 호스트 이름이 계속 바뀌어 목적이 훼손됩니다.
- `podAntiAffinity`를 hostname에 required로 두면 **replica 수가 가용 노드 수를 넘을 수 없습니다.** HPA `maxReplicas`를 그에 맞추세요.
- `pod_association`은 `from: connection`(TCP 소스 IP)을 씁니다 — Datadog 프로토콜 페이로드에는 `k8s.pod.ip` 리소스 속성이 없습니다.

### 헬스체크

Collector의 `health_check` extension을 켜고 probe에 쓰면, 트레이스 포트 TCP 체크보다 정확합니다.

## 7. 워크로드 전환 — 플래그가 아니라 경로

공유 애플리케이션 차트에 **목적지 선택**을 넣는 것을 권합니다.

```yaml
components:
  <component>:
    datadog:
      apm:
        route: gateway     # gateway | node (기본 node)
```

**컴포넌트 → 릴리스/클러스터 → `node`(기본)** 순으로 해석합니다.

| 경우 | 결과 |
|---|---|
| 컴포넌트만 `gateway` | 해당 컴포넌트만 전환 |
| 클러스터 기본값 `gateway` | 전부 전환 |
| 기본값 `gateway` · 특정 컴포넌트 `node` | 그 컴포넌트만 노드 로컬 |
| 잘못된 값 | **렌더 실패** (조용히 노드 로컬로 떨어지지 않음) |

### 왜 on/off 플래그가 아닌가

`enabled: true`만 있으면 "게이트웨이로 보낸다"는 표현만 가능하고, 부재가 곧 노드 로컬입니다. 게이트웨이가 예외인 동안은 괜찮지만, **대부분이 옮겨가 기본값을 뒤집는 순간 틀린 설계가 됩니다** — APM→호스트 피벗이 꼭 필요한 워크로드가 다시 빠져나올 방법이 없습니다.

### 렌더 내용

```yaml
- name: DD_TRACE_AGENT_URL
  value: "http://<gateway-service>:8126"
- name: DD_TAGS
  value: "k8s.container.name:<component>"
```

**워크로드가 주는 것은 컨테이너 이름 하나뿐**입니다. 나머지는 게이트웨이가 K8s API에서 해석합니다.

### strictly opt-in은 검증하세요

`route`가 없으면 **아무것도 렌더되지 않아야** 합니다. 그래야 미전환 워크로드의 pod 템플릿 해시가 그대로라 재시작이 없습니다.

단언하지 말고 확인하세요 — **values를 고정한 채 차트만 신/구로 바꿔** 전체 앱을 렌더해 비교하면 됩니다. values까지 같이 비교하면 배포 자동화의 이미지 태그 드리프트가 섞여 오탐이 납니다.

`deployment` `rollout` `cronjob` `job` 네 종류 모두에 배선해야 합니다. **단명 태스크 워크로드가 이 구조의 주 대상**이므로 cronjob·job을 빼면 기능이 자기 용도를 표현하지 못합니다.

## 8. 적용 절차

1. Collector 차트 배포 — 고정 노드, 자체 배포 단위
2. API 키 시크릿 연결 확인
3. 공유 차트에 `route` 경로 추가, 게이트웨이 URL은 클러스터 공통값으로 한 번만 정의
4. **트래픽이 적고 위험이 낮은 실제 워크로드 하나**로 파일럿
5. 검증 후 워크로드 단위로 확대

### 파일럿 대상 고르기

- 실제 프로덕션 워크로드이되 **트래픽이 적은 것** — 검증은 되면서 위험은 낮게
- health check가 아닌 **진짜 애플리케이션 span**을 내는 것
- 자식 span·아웃바운드 호출이 있으면 필터 가드 검증에 유리
- **격리된 네임스페이스는 피하세요** — in-cluster egress를 막는 NetworkPolicy가 붙은 워크로드는 게이트웨이로 보내면 그 설계 의도와 충돌합니다

### 검증 항목

| 확인 | 기대값 |
|---|---|
| `host` | 게이트웨이 호스트 이름 |
| 컨테이너 태그 | `container_id` `kube_container_name` `image_name` `image_tag` 네이티브 |
| operation name | 기존과 동일 |
| APM 지표 | `trace.<op>.hits` 기존 이름으로 생성 |
| 자식 span | 보존 |

**롤백**은 `route` 제거 또는 `node`. 다음 pod 기동부터 복귀합니다.

## 9. 운영 시 주의

- **필터 패턴은 노드 에이전트와 동기화**하세요. 한쪽만 바꾸면 같은 앱이 경로에 따라 다르게 필터링됩니다.
- **Collector 설정 변경은 이미지로 검증**하세요. 설정 오류는 기동 실패로만 드러납니다.

  ```shell
  docker run --rm -e DD_API_KEY=dummy -v <rendered-config>:/conf/config.yaml:ro \
    ghcr.io/open-telemetry/opentelemetry-collector-releases/opentelemetry-collector-contrib:<version> \
    validate --config=/conf/config.yaml
  ```

- 필터 동작도 실제로 확인할 수 있습니다 — 로컬에서 Collector를 띄우고 `debug` exporter로 붙인 뒤 payload를 보내면 무엇이 통과하고 무엇이 버려지는지 그대로 보입니다.
- **Datadog이 트레이스 백엔드가 아닌 환경에는 의미가 없습니다.**

## 10. 비용 관점 점검

도입 전에 **무엇이 실제로 청구를 만드는지** 먼저 측정하세요. span의 `host` 고유값을 시간 단위로 집계하면 근사치를 얻을 수 있습니다.

상위 시간값들이 **반복되는 배치 작업**에 몰려 있다면, 게이트웨이보다 **그 배치의 형태를 바꾸는 쪽이 싸게 큰 몫을 가져갈 수 있습니다.** 월 8시간까지는 무료이므로, 자주 도는 짧은 버스트를 드물고 큰 버스트로 모으면 그 비용이 0이 됩니다.

다만 이는 배치 일정을 관측 비용에 종속시키는 것이고 단명 워크로드가 늘면 한계가 옵니다. **구조적 해법은 게이트웨이 쪽**입니다. 둘은 배타적이지 않습니다.

## 참고 자료

- [APM 과금](https://docs.datadoghq.com/account_management/billing/apm_tracing_profiler/) — HWM, 9번째 시간값. Fargate는 월 평균 기준
- [Operation name 매핑 마이그레이션](https://docs.datadoghq.com/opentelemetry/migrate/migrate_operation_names/) — `operation.name` 최우선순위
- [OTel semantic mapping](https://docs.datadoghq.com/opentelemetry/schema_semantics/semantic_mapping/) — 리소스 속성 → 네이티브 Datadog 태그
- [Hostname and tagging](https://docs.datadoghq.com/opentelemetry/config/hostname_tagging/) · [Hostname 해석](https://docs.datadoghq.com/opentelemetry/schema_semantics/hostname/)
- [k8sattributes processor](https://github.com/open-telemetry/opentelemetry-collector-contrib/blob/main/processor/k8sattributesprocessor/README.md) — 컨테이너 속성 요구 조건, `from: node`
- [datadogreceiver](https://github.com/open-telemetry/opentelemetry-collector-contrib/blob/main/receiver/datadogreceiver/README.md) — 기존 트레이서 재계측 불필요
- [Collector 배포 패턴](https://docs.datadoghq.com/opentelemetry/setup/collector_exporter/deploy/) — 표에서 gateway의 Traces 칸이 빈 것은 **앞단에 노드 agent가 없는 구성**을 가리키며, 여기서 쓰는 토폴로지와 다릅니다

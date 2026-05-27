# EC2 nginx 튜닝 (Union Ledger)

앱 코드는 `/opt/union-ledger`, **nginx 설정은 `/etc/nginx/`** 에 있습니다.  
`nginx.conf` 맨 아래 기본 `server { listen 80; ... }` 는 **건드리지 말고**, 사이트 설정은 **`conf.d`** 에 둡니다.

## 어디를 고치나

| 파일 | 역할 |
|------|------|
| `/etc/nginx/nginx.conf` | 전역 설정 (대부분 유지) |
| `/etc/nginx/conf.d/union-ledger-api.conf` | **도메인·HTTPS·프록시** (여기 수정) |
| `deploy/nginx/union-ledger.kro.kr.conf` | 레포 참고용 템플릿 |

Certbot이 이미 SSL 줄을 넣었다면, **443 `server` 블록 안**에만 헤더·타임아웃을 추가해도 됩니다.

## 적용

```bash
# EC2
sudo cp /opt/union-ledger/deploy/nginx/union-ledger.kro.kr.conf \
  /etc/nginx/conf.d/union-ledger-api.conf

sudo nginx -t
sudo systemctl reload nginx
```

로컬에서 배포 후:

```bash
./scripts/deploy.sh   # rsync로 deploy/nginx/ 포함
```

## 튜닝 요약

| 설정 | 의미 |
|------|------|
| `return 301 https://...` | HTTP 접속 시 HTTPS로 이동 |
| `/.well-known/acme-challenge/` | 인증서 **갱신**용 (certbot) |
| `client_max_body_size 50M` | 증빙/엑셀 업로드 허용 크기 |
| `proxy_read_timeout 300s` | OCR 등 긴 API 대기 |
| `Strict-Transport-Security` | 브라우저가 HTTPS만 쓰도록 유도 |
| `gzip` | JSON 응답 압축 (선택) |

## 확인

```bash
curl -I http://union-ledger.kro.kr/api/v1/health    # 301 → https
curl https://union-ledger.kro.kr/api/v1/health     # 200
sudo certbot renew --dry-run
```

## 주의

- `kro.kr` Let’s Encrypt **주간 발급 한도** — 불필요하게 `certbot` 반복 실행하지 않기.
- `api.union-ledger.kro.kr` 을 쓰려면 `server_name` 과 `certbot -d` 에 **서브도메인 추가** 필요.

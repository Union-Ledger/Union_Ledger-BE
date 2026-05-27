# EC2 배포 (Terraform + Amazon Linux 2023)

발표·의사결정 정리: [docs/PRESENTATION_DEPLOYMENT.md](../docs/PRESENTATION_DEPLOYMENT.md) (후보 비교, Cloud Run, nginx, 현재 상태)

## 사전 준비

1. [AWS CLI](https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html) + 자격 증명 (`aws configure`)
2. [Terraform](https://developer.hashicorp.com/terraform/install) >= 1.5
3. EC2 **키 페어** 생성 (`.pem` 파일 보관)
4. 본인 공인 IP 확인 후 `allowed_ssh_cidr`에 `/32`로 설정

## 1) 인프라 생성

```bash
cd terraform
cp terraform.tfvars
# terraform.tfvars 편집 (key_name, allowed_ssh_cidr 필수)

terraform init
terraform plan
terraform apply
```

출력된 `public_ip`, `docs_url` 확인.

## 2) 서버 `.env` 준비 (로컬)

프로젝트 루트에 프로덕션용 `.env` (Git에 올리지 않음):

```bash
cp .env.production .env
# JWT_SECRET_KEY, POSTGRES_PASSWORD, ALLOWED_ORIGINS 수정
# DATABASE_URL 호스트는 postgres (Docker 서비스명)
```

## 3) 코드 배포 (딸칙)

프로젝트 루트에서:

```bash
export DEPLOY_KEY=~/.ssh/my-keypair.pem
./scripts/deploy.sh
```

- 소스 rsync → EC2 `/opt/union-ledger`
- `docker compose -f docker-compose.prod.yml up -d --build`
- `alembic upgrade head`

## 4) 감사 시즌 종료 후

```bash
cd terraform
terraform destroy   # EC2·VPC 등 삭제 (EBS 스냅샷 정책은 콘솔에서 확인)
```

또는 AWS 콘솔에서 인스턴스만 **Stop** (공인 IP는 재시작 시 바뀔 수 있음).

## HTTPS (nginx on EC2)

프론트(HTTPS) 연동은 **EC2 호스트에 nginx**로 TLS 종료 후 `127.0.0.1:8000`(Docker API)으로 프록시합니다.

1. 내도메인: **A** `@` 또는 `union-ledger` → EC2 `public_ip`
2. 보안 그룹: `expose_web_ports = true` — **80·443** (`terraform apply`)
3. EC2: `sudo certbot --nginx -d union-ledger.kro.kr`
4. 튜닝(HTTP→HTTPS, 업로드 크기, HSTS 등): [docs/nginx-ec2.md](../docs/nginx-ec2.md), 템플릿 `deploy/nginx/union-ledger.kro.kr.conf`

설정 파일은 **`/etc/nginx/conf.d/`** (홈 `~` 아님). 배포 후 `./scripts/deploy.sh` 로 앱만 갱신하면 됩니다.

## 보안 권장

- `allowed_ssh_cidr`는 본인 IP/32만
- nginx 사용 시 `expose_api_port = false` 로 8000 외부 차단 가능
- **5432**는 보안 그룹에 열지 않음 (Compose 내부만)

output "instance_id" {
  description = "EC2 instance ID"
  value       = aws_instance.app.id
}

output "public_ip" {
  description = "EC2 public IP (may change after stop/start)"
  value       = aws_instance.app.public_ip
}

output "ssh_command" {
  description = "Example SSH command (Amazon Linux default user: ec2-user)"
  value       = "ssh -i <path-to-private-key.pem> ec2-user@${aws_instance.app.public_ip}"
}

output "api_url" {
  description = "API base URL (HTTP, port 8000)"
  value       = "http://${aws_instance.app.public_ip}:8000"
}

output "docs_url" {
  description = "Swagger UI"
  value       = "http://${aws_instance.app.public_ip}:8000/docs"
}

output "app_install_dir" {
  description = "Remote app directory used by scripts/deploy.sh"
  value       = var.app_install_dir
}

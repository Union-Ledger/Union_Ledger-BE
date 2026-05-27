# This AWS account/region has no default VPC — create one so EC2 + SG can attach.
resource "aws_default_vpc" "default" {
  tags = {
    Name = "${var.project_name}-default-vpc"
  }
}

data "aws_subnets" "default" {
  filter {
    name   = "vpc-id"
    values = [aws_default_vpc.default.id]
  }
}

data "aws_subnet" "default" {
  id = data.aws_subnets.default.ids[0]
}

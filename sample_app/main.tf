terraform {
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }
}

provider "aws" {
  region = "eu-west-1"
}

variable "db_password" {
  type      = string
  sensitive = true
}

# --- Object storage -------------------------------------------------------
resource "aws_s3_bucket" "uploads" {
  bucket = "egress-demo-uploads"
}

# --- Managed Postgres -----------------------------------------------------
resource "aws_db_instance" "metadata" {
  identifier          = "egress-demo-db"
  engine              = "postgres"
  engine_version      = "16.3"
  instance_class      = "db.t4g.micro"
  allocated_storage   = 20
  db_name             = "uploads"
  username            = "app"
  password            = var.db_password
  skip_final_snapshot = true
}

# --- Queue ----------------------------------------------------------------
resource "aws_sqs_queue" "jobs" {
  name                       = "egress-demo-jobs"
  visibility_timeout_seconds = 60
}

# --- Worker (Lambda) ------------------------------------------------------
resource "aws_iam_role" "worker" {
  name = "egress-demo-worker"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "lambda.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy_attachment" "worker_sqs" {
  role       = aws_iam_role.worker.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaSQSQueueExecutionRole"
}

resource "aws_lambda_function" "worker" {
  function_name = "egress-demo-worker"
  role          = aws_iam_role.worker.arn
  runtime       = "python3.11"
  handler       = "lambda_function.lambda_handler"
  filename      = "worker.zip"
  timeout       = 30

  environment {
    variables = {
      RDS_ENDPOINT = aws_db_instance.metadata.address
      DB_NAME      = "uploads"
      DB_USER      = "app"
      DB_PASSWORD  = var.db_password
    }
  }
}

resource "aws_lambda_event_source_mapping" "jobs_to_worker" {
  event_source_arn = aws_sqs_queue.jobs.arn
  function_name    = aws_lambda_function.worker.arn
  batch_size       = 10
}

resource "aws_cloudwatch_log_group" "api" {
  name              = "/egress-demo/api"
  retention_in_days = 14
}

output "rds_endpoint" {
  value = aws_db_instance.metadata.address
}

output "sqs_queue_url" {
  value = aws_sqs_queue.jobs.url
}

variable "env" {
  type        = string
  description = "The env we are in: dev,qa,stage,prod"
}

variable "image" {
  type    = string
  default = "nardoarevalywaymark/postgres-mcp:v0.3.3"
}

variable "port" {
  type        = number
  description = "The internal port that postgres-mcp is running on."
  default     = 8000
}

variable "cpu" {
  type        = number
  description = "How much cpu to give postgres-mcp"
  default     = 100
}

variable "memory" {
  type        = number
  description = "How much memory to give postgres-mcp"
  default     = 512
}


job "postgresmcp" {
  datacenters = ["*"]

  group "app" {
    count = 1

    network {
      mode = "bridge"
      port "http" {
        to = var.port
      }
    }

    service {
      name = "postgresmcp"
      port = "http"

      tags = [
        "traefik.enable=true",
        "traefik.consulcatalog.connect=true",
        "traefik.http.routers.postgres-mcp.tls=true",
        "traefik.http.routers.postgres-mcp.rule=Host(`coredb-mcp.${var.env}.waymarkcare.in`)",
      ]

      connect {
        sidecar_service {
          proxy {
            local_service_port = var.port
          }
        }
      }

      check {
        type     = "http"
        path     = "/health"
        interval = "10s"
        timeout  = "10s"
      }
    }

    vault {
      policies = [
        "${var.env}-waymark-core-db-ro",
      ]
      change_mode  = "noop"
      env          = false
      disable_file = false
    }

    task "app" {
      driver = "docker"

      config {
        image = var.image
        ports = ["http"]
        args = [
          "--access-mode=restricted",
          "--output-directory=/app/output",
          "--transport", "http",
          "--sse-host", "0.0.0.0",
          "--sse-port", "${var.port}"
        ]
      }

      template {
        data = <<-EOF
          ENV=${var.env}
          TOOL_IDENTIFIER=core_
          {{ with secret "database/${var.env}/waymark-core/creds/ro-${var.env}_waymark-core-db" -}}
          DATABASE_URI=postgresql://{{ .Data.username }}:{{ .Data.password | toJSON }}@coredb${var.env == "prod" ? "-ro" : ""}.${var.env}.waymarkcare.in:5432/waymark-core-db
          {{ end }}
        EOF

        destination = "${NOMAD_SECRETS_DIR}/secrets.env"
        env         = true
        change_mode = "restart"
        perms       = 600
      }


      resources {
        cpu    = var.cpu
        memory = var.memory
      }
    }
  }
}

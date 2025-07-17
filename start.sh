#!/bin/bash

# IP2Location LITE Setup Script
# This script helps you get started with IP2Location Docker Compose setup

set -e

echo "🌍 IP2Location LITE Docker Compose Setup"
echo "========================================="

# Check if Docker and Docker Compose are installed
if ! command -v docker &> /dev/null; then
    echo "❌ Docker is not installed. Please install Docker first."
    exit 1
fi

# Check for Docker Compose command
if command -v docker-compose &>/dev/null; then
    COMPOSE_CMD="docker-compose"
elif docker compose version &>/dev/null; then
    COMPOSE_CMD="docker compose"
else
    echo "❌ Docker Compose is not installed. Please install it first."
    exit 1
fi

echo "✅ Docker and Docker Compose found"

# Create .env file if it doesn't exist
if [ ! -f .env ]; then
    echo "📝 Creating .env file from template..."
    cp environment.example .env
    
    # Generate random API key
    API_KEY=$(openssl rand -base64 32 | tr -d "=+/" | cut -c1-32)
    
    # Update .env file with generated values
    sed -i.bak "s/your_secure_api_key/$API_KEY/g" .env
    
    # Remove backup file
    rm .env.bak 2>/dev/null || true
    
    echo "✅ Generated secure API key"
    echo "📋 Your API key is: $API_KEY"
    echo "   (Also saved in .env file)"
else
    echo "✅ .env file already exists"
    API_KEY=$(grep "^API_KEY=" .env | cut -d'=' -f2)
fi

# Create necessary directories
echo "📁 Creating directories..."
mkdir -p api/logs updater/logs database/{backups,downloads} db

# Check if services are already running
if ${COMPOSE_CMD} ps | grep -q "Up"; then
    echo "⚠️  Some services are already running"
    read -p "Do you want to restart them? (y/N): " -n 1 -r
    echo
    if [[ $REPLY =~ ^[Yy]$ ]]; then
        echo "🔄 Stopping existing services..."
        ${COMPOSE_CMD} down
    else
        echo "ℹ️  Keeping existing services running"
        exit 0
    fi
fi

# Start services
echo "🚀 Starting IP2Location services..."
${COMPOSE_CMD} up -d

echo "⏳ Waiting for services to start..."
sleep 10

# Wait for API to be ready
echo "🔍 Checking service health..."
max_attempts=30
attempt=1

while [ $attempt -le $max_attempts ]; do
    if curl -s http://localhost:8080/health > /dev/null 2>&1; then
        echo "✅ Services are ready!"
        break
    fi
    
    if [ $attempt -eq $max_attempts ]; then
        echo "❌ Services failed to start within expected time"
        echo "   Check logs with: ${COMPOSE_CMD} logs"
        exit 1
    fi
    
    echo "   Attempt $attempt/$max_attempts - waiting..."
    sleep 2
    ((attempt++))
done

# Test API
echo "🧪 Testing API..."
response=$(curl -s "http://localhost:8080/?api_key=$API_KEY&ip=8.8.8.8")
if echo "$response" | grep -q "country_code"; then
    echo "✅ API test successful!"
else
    echo "⚠️  API test failed - database might still be updating"
    echo "   This is normal on first startup. The database will be available shortly."
fi

echo ""
echo "🎉 IP2Location LITE setup complete!"
echo ""
echo "📊 Service Status:"
${COMPOSE_CMD} ps
echo ""
echo "🔗 Access Points:"
echo "   • Main API:     http://localhost:${API_PORT:-8080}/?ip=8.8.8.8"
echo "   • Health Check: http://localhost:8080/health"
echo "   • API Info:     http://localhost:8080/api/v1/info"
echo "   • Test Lookup:  curl \"http://localhost:${API_PORT:-8080}/?api_key=$API_KEY&ip=8.8.8.8\""
echo ""
echo "📋 Your API Key: $API_KEY"
echo ""
echo "📚 Documentation:"
echo "   • Full API docs in README.md"
echo "   • Check logs: ${COMPOSE_CMD} logs [service-name]"
echo "   • Stop services: ${COMPOSE_CMD} down"
echo ""
echo "⚠️  Important Notes:"
echo "   • The initial database download may take a few minutes"
echo "   • Database updates happen daily based on your UPDATE_SCHEDULE"
echo "   • Database files are stored in the ./db/ directory"
echo "   • This service is optimized for the high-performance binary database format"
echo "   • Service runs directly on port ${API_PORT:-8080} (no reverse proxy)"
echo "   • Backup your .env file - it contains your API key and settings"
echo ""
echo "🔍 Monitor the initial database download:"
echo "   ${COMPOSE_CMD} logs -f ip2location-updater"
echo ""

# Check if database is empty and suggest monitoring
if [ -z "$(ls -A ./db)" ]; then
    echo "📥 Database not found - initial download in progress..."
    echo "   Monitor progress: ${COMPOSE_CMD} logs -f ip2location-updater"
fi 
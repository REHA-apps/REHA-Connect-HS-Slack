param (
    [string]$Region = "eu-west-1",
    [string]$AccountId = "328833518397",
    [string]$FunctionName = "reha-slack-connect"
)

$EcrUri = "$AccountId.dkr.ecr.$Region.amazonaws.com"
$ImageUri = "$EcrUri/$FunctionName`:latest"

Write-Host "Starting deployment to AWS Lambda ($FunctionName)..." -ForegroundColor Cyan

# 1. Authenticate Docker to AWS ECR
Write-Host "`nAuthenticating with AWS ECR..." -ForegroundColor Yellow
aws ecr get-login-password --region $Region | docker login --username AWS --password-stdin $EcrUri
if ($LASTEXITCODE -ne 0) {
    Write-Host "Failed to authenticate with AWS ECR. Check your AWS credentials." -ForegroundColor Red
    exit $LASTEXITCODE
}

# 1.5 Ensure ECR repository exists
Write-Host "`n📦 Ensuring ECR repository exists..." -ForegroundColor Yellow
aws ecr describe-repositories --repository-names $FunctionName --region $Region 2>$null
if ($LASTEXITCODE -ne 0) {
    aws ecr create-repository --repository-name $FunctionName --region $Region | Out-Null
    Write-Host "   ✅ Repository created: $FunctionName" -ForegroundColor Green
} else {
    Write-Host "   ✅ Repository already exists." -ForegroundColor Green
}

# 2. Build the Docker image
Write-Host "`nBuilding Docker image..." -ForegroundColor Yellow
docker buildx build --provenance=false -f Dockerfile.lambda -t $FunctionName`:latest .
if ($LASTEXITCODE -ne 0) {
    Write-Host "Docker build failed." -ForegroundColor Red
    exit $LASTEXITCODE
}

# 3. Tag and Push to ECR
Write-Host "`nTagging and pushing image to ECR..." -ForegroundColor Yellow
docker tag $FunctionName`:latest $ImageUri
docker push $ImageUri
if ($LASTEXITCODE -ne 0) {
    Write-Host "Docker push failed." -ForegroundColor Red
    exit $LASTEXITCODE
}

# 4. Create or Update Lambda Function
Write-Host "`n⚡ Deploying Lambda function..." -ForegroundColor Yellow
$FunctionExists = aws lambda get-function --function-name $FunctionName --region $Region 2>$null
if ($LASTEXITCODE -ne 0) {
    Write-Host "   Creating new Lambda function '$FunctionName'..." -ForegroundColor Yellow
    aws lambda create-function `
        --function-name $FunctionName `
        --package-type Image `
        --code ImageUri=$ImageUri `
        --role "arn:aws:iam::${AccountId}:role/reha-connect-lambda-role" `
        --memory-size 1024 `
        --timeout 30 `
        --architectures arm64 `
        --region $Region | Out-Null
} else {
    Write-Host "   Updating existing Lambda function code..." -ForegroundColor Yellow
    aws lambda update-function-code --function-name $FunctionName --image-uri $ImageUri --region $Region | Out-Null
}

if ($LASTEXITCODE -ne 0) {
    Write-Host "❌ Failed to create or update Lambda function." -ForegroundColor Red
    exit $LASTEXITCODE
}

Write-Host "`nDeployment complete! The new code is now live at api.rehaapps.com" -ForegroundColor Green

Write-Host "`nWaiting for AWS Lambda to finish processing the new code before updating environment variables..." -ForegroundColor Cyan
aws lambda wait function-updated --function-name $FunctionName --region $Region

# 5. Update Lambda Environment Variables
if (Test-Path "lambda-env.json") {
    Write-Host "`nUpdating Lambda environment variables from lambda-env.json..." -ForegroundColor Yellow
    aws lambda update-function-configuration --function-name $FunctionName --environment file://lambda-env.json --region $Region | Out-Null
    if ($LASTEXITCODE -ne 0) {
        Write-Host "Failed to update Lambda environment variables. Code is deployed but config is unchanged." -ForegroundColor Red
    } else {
        Write-Host "Environment variables updated successfully!" -ForegroundColor Green
    }
}

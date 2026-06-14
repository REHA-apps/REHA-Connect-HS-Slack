param (
    [string]$Region      = "eu-west-1",
    [string]$AccountId   = "328833518397",
    [string]$FunctionName = "reha-slack-connect-sentiment"
)

$EcrUri   = "$AccountId.dkr.ecr.$Region.amazonaws.com"
$ImageUri = "$EcrUri/$FunctionName`:latest"

Write-Host "🧠 Deploying Sentiment Lambda ($FunctionName)..." -ForegroundColor Cyan

# 1. Authenticate Docker to AWS ECR
Write-Host "`n🔑 Authenticating with AWS ECR..." -ForegroundColor Yellow
aws ecr get-login-password --region $Region | docker login --username AWS --password-stdin $EcrUri
if ($LASTEXITCODE -ne 0) {
    Write-Host "❌ ECR authentication failed." -ForegroundColor Red
    exit $LASTEXITCODE
}

# 2. Create ECR repository if it doesn't exist yet
Write-Host "`n📦 Ensuring ECR repository exists..." -ForegroundColor Yellow
aws ecr describe-repositories --repository-names $FunctionName --region $Region 2>$null
if ($LASTEXITCODE -ne 0) {
    aws ecr create-repository --repository-name $FunctionName --region $Region | Out-Null
    Write-Host "   ✅ Repository created: $FunctionName" -ForegroundColor Green
} else {
    Write-Host "   ✅ Repository already exists." -ForegroundColor Green
}

# 3. Build the Sentinel Lambda image
# NOTE: --provenance=false is required so Lambda's image runtime
#       doesn't reject OCI attestation manifest lists.
Write-Host "`n🔨 Building Docker image..." -ForegroundColor Yellow
docker buildx build --provenance=false `
    -f functions/sentiment/Dockerfile `
    -t $FunctionName`:latest .
if ($LASTEXITCODE -ne 0) {
    Write-Host "❌ Docker build failed." -ForegroundColor Red
    exit $LASTEXITCODE
}

# 4. Tag and push to ECR
Write-Host "`n🏷️ Tagging and pushing image to ECR..." -ForegroundColor Yellow
docker tag $FunctionName`:latest $ImageUri
docker push $ImageUri
if ($LASTEXITCODE -ne 0) {
    Write-Host "❌ Docker push failed." -ForegroundColor Red
    exit $LASTEXITCODE
}

# 5. Create or update the Lambda function
Write-Host "`n⚡ Deploying Lambda function..." -ForegroundColor Yellow
$FunctionExists = aws lambda get-function --function-name $FunctionName --region $Region 2>$null
if ($LASTEXITCODE -ne 0) {
    # First deploy — create the function
    Write-Host "   Creating new Lambda function '$FunctionName'..." -ForegroundColor Yellow

    # Read SUPABASE_URL and SUPABASE_KEY from lambda-env.json (same file used by deploy.ps1)
    $LambdaEnvFile = Join-Path $PSScriptRoot "lambda-env.json"
    if (-not (Test-Path $LambdaEnvFile)) {
        Write-Host "❌ lambda-env.json not found at $LambdaEnvFile" -ForegroundColor Red
        exit 1
    }
    $LambdaEnv = Get-Content $LambdaEnvFile | ConvertFrom-Json
    $SupabaseUrl = $LambdaEnv.Variables.SUPABASE_URL
    $SupabaseKey = $LambdaEnv.Variables.SUPABASE_KEY

    if (-not $SupabaseUrl -or -not $SupabaseKey) {
        Write-Host "❌ SUPABASE_URL or SUPABASE_KEY missing from lambda-env.json" -ForegroundColor Red
        exit 1
    }

    $EnvVars = "Variables={SUPABASE_URL=$SupabaseUrl,SUPABASE_KEY=$SupabaseKey,HF_HUB_DISABLE_TELEMETRY=1}"

    aws lambda create-function `
        --function-name $FunctionName `
        --package-type Image `
        --code ImageUri=$ImageUri `
        --role "arn:aws:iam::${AccountId}:role/reha-connect-lambda-role" `
        --memory-size 2048 `
        --timeout 60 `
        --architectures arm64 `
        --environment $EnvVars `
        --region $Region | Out-Null
} else {
    # Subsequent deploys — update image only
    aws lambda update-function-code `
        --function-name $FunctionName `
        --image-uri $ImageUri `
        --region $Region | Out-Null
}

if ($LASTEXITCODE -ne 0) {
    Write-Host "❌ Lambda deployment failed." -ForegroundColor Red
    exit $LASTEXITCODE
}

# 6. Print the ARN so you can set SENTIMENT_LAMBDA_ARN on the main Lambda
Write-Host "`n🔗 Fetching Lambda ARN..." -ForegroundColor Yellow
$Arn = aws lambda get-function --function-name $FunctionName --region $Region `
    --query "Configuration.FunctionArn" --output text
Write-Host "`n✅ Sentiment Lambda deployed!" -ForegroundColor Green
Write-Host ""
Write-Host "Next step — set this env var on the main Lambda:" -ForegroundColor Cyan
Write-Host "  SENTIMENT_LAMBDA_ARN=$Arn" -ForegroundColor White
Write-Host ""
Write-Host "  aws lambda update-function-configuration \" -ForegroundColor DarkGray
Write-Host "    --function-name reha-slack-connect \" -ForegroundColor DarkGray
Write-Host "    --environment `"Variables={SENTIMENT_LAMBDA_ARN=$Arn,...}`"" -ForegroundColor DarkGray

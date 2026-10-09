# Copy to the EC2 instance
# Ensure "moments-tool-ec2" alias set in ~/.ssh/config
scp -r .env docker-compose.yml scripts config moments-tool-ec2:~/moments/AI4ME_TOOL

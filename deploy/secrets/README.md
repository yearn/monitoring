# deploy/secrets

SOPS-managed secrets for the production `yearn-monitor` service. Plaintext
`*.env` files in this directory are gitignored; only `*.env.enc` (SOPS output)
and `*.env.example` (the placeholder template) should ever land in git.

## First-time setup

```sh
# 1) generate an age keypair on your workstation
age-keygen -o ~/.config/sops/age/keys.txt
chmod 600 ~/.config/sops/age/keys.txt

# 2) print your public key (starts with age1…)
age-keygen -y ~/.config/sops/age/keys.txt

# 3) add it to ../../.sops.yaml under creation_rules[].age (replace the
#    REPLACE-ME placeholder for the first operator), then re-key any existing
#    encrypted file:
sops updatekeys prod.env.enc
```

## Encrypt a fresh env file

```sh
cp prod.env.example prod.env
$EDITOR prod.env                      # fill in real values
sops -e --input-type dotenv --output-type dotenv prod.env > prod.env.enc
rm prod.env                           # NEVER commit plaintext
git add prod.env.enc
```

## On the VPS

The `yearn-monitor.service` `ExecStartPre` decrypts `prod.env.enc` to
`/etc/yearn-monitoring/.env` using the age key at `/etc/yearn-monitoring/age.key`
whenever the encrypted file is newer than the existing `.env`. To do it by hand:

```sh
SOPS_AGE_KEY_FILE=/etc/yearn-monitoring/age.key \
  sops -d --input-type dotenv --output-type dotenv prod.env.enc \
  | sudo tee /etc/yearn-monitoring/.env > /dev/null
sudo chown root:<deploy-user> /etc/yearn-monitoring/.env
sudo chmod 640 /etc/yearn-monitoring/.env
```

# nutri-checker

A deterministic food checker for diabetes plus kidney-friendly eating, built for use by an AI agent skill. It gives a green/yellow/red verdict against daily targets, plus meal, weight and activity logs. Python stdlib only.

```
python3 nutri.py lookup "sweet potato"
python3 nutri.py check sweet_potato:0.5
python3 nutri.py log congee egg --meal breakfast
python3 nutri.py weight 50.2
python3 nutri.py today
```

`profile.json` holds EXAMPLE targets, and the values in `foods.json` are rounded approximations. This is not medical advice.

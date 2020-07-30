import gkeepapi
import json


filename = './config.json'
with open(filename, 'r') as jsonFile:
    config = json.load(jsonFile)
    print(config)


keep = gkeepapi.Keep()
success = keep.login(config['username'], config['password'])

note = keep.createNote('Hello Baby', 'I love you')
note.pinned = True
note.color = gkeepapi.node.ColorValue.Red
keep.sync()
# MongoMultiMaster

This is a tool which allows you to set up multi-master replication with
MongoDB. It is emphatically *not* supported by 10gen, the makers of MongoDB.

It works by querying the oplog on one replica set and applying its operations to
another replica set. It supports bidirectional replication by tagging each
document replicated with its source.

## Installing

To install, use pip:

	sudo apt-get install python-yaml python-gevent
	cd mmm
	python setup.py sdist
    sudo pip install dist/MongoMultiMaster-0.0.4dev.tar.gz
    
## MongoDB Setup

MMM needs access to the replica set oplog for each master. This means it doesn't
work with servers that are configured as standalone servers. To convert a
standalone server to a singleton replica set, first you need to tell it about the
set it's in at launch time:

~~~~
$ mongod --replSet foo
~~~~

Then, to start the replica set, you need to execute the following at the `mongo`
shell:

~~~~
> rs.initiate()
~~~~

## MongoMultiMaster Replication Setup

Once you've created the replica set master, you'll need to tell MMM where the
servers are. This is done through a YAML config file. A sample config file with
two servers is included below:

~~~~
server_a:
  id: '2c88ae84-7cb9-40f7-835d-c05e981f564d'
  uri: 'mongodb://localhost:27019'
server_b:
  id: '0d9c284b-b47c-40b5-932c-547b8685edd0'
  uri: 'mongodb://localhost:27017'
~~~~

First, let's verify that there's no configuration and that we can connect to all
the servers in the config file:

~~~~
$ mmm -c test.yml clear-config
About to clear config on servers: ['server_a', 'server_b'], are you sure? (yN) y
Clear config for server_a
Clear config for server_b
$ mmm -c test.yml dump-config
=== Server Config ===
server_a (2c88ae84-7cb9-40f7-835d-c05e981f564d) => mongodb://localhost:27019
server_b (0d9c284b-b47c-40b5-932c-547b8685edd0) => mongodb://localhost:27017

=== server_a Replication Config
=== server_b Replication Config

~~~~

Next, we'll set up two replicated collections:

~~~~
$ mmm -c test.yml replicate --src=server_a/test.foo --dst=server_b/test.foo
$ mmm -c test.yml replicate --src=server_a/test.bar --dst=server_b/test.bar
~~~~

And confirm they're configured correctly:

~~~~
$ mmm -c test.yml dump-config
=== Server Config ===
server_a (2c88ae84-7cb9-40f7-835d-c05e981f564d) => mongodb://localhost:27019
server_b (0d9c284b-b47c-40b5-932c-547b8685edd0) => mongodb://localhost:27017

=== server_a Replication Config
=== server_b Replication Config
     - test.foo <= server_a/test.foo
     - test.bar <= server_a/test.bar
~~~~

Now, let's make the replication bidirectional:

~~~~
$ mmm -c test.yml replicate --src=server_b/test.foo --dst=server_a/test.foo
$ mmm -c test.yml replicate --src=server_b/test.bar --dst=server_a/test.bar
~~~~

And verify that it's correct...

~~~~
$ mmm -c test.yml dump-config
=== Server Config ===
server_a (2c88ae84-7cb9-40f7-835d-c05e981f564d) => mongodb://localhost:27019
server_b (0d9c284b-b47c-40b5-932c-547b8685edd0) => mongodb://localhost:27017

=== server_a Replication Config
     - test.foo <= server_b/test.foo
     - test.bar <= server_b/test.bar
=== server_b Replication Config
     - test.foo <= server_a/test.foo
     - test.bar <= server_a/test.bar
~~~~

Now we can run the replicator:

~~~~
$ mmm -c test.yml run
~~~~

## Things to Consider

- Replication can fall behind if you're writing a lot. This is not handled at
  all.
- Replication begins at the time when `mmm run` was first called. You should be
  able to stop/start `mmm` and have it pick up where it left off.
- Conflicts between masters aren't handled; if you're writing to the same
  document on both heads frequently, you can get out of sync.
- Replication inserts a bookkeeping field into each document to signify the
  server UUID that last wrote the document. This expands the size of each
  document slightly.


There are probably sharp edges, other missed bugs, and various nasty things
waiting for you if you use MMM in a production system without thorough
testing. But if you like running with scissors and otherwise living dangerously,
feel free to try it out.

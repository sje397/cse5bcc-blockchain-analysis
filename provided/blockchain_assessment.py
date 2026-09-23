import sys
import hashlib
import json

from time import time
from uuid import uuid4

from flask import Flask, jsonify, request

import requests
from urllib.parse import urlparse

class Blockchain(object):
    difficulty_target= "0000"

    def hash_block(self, block):
        block_encoded= json.dumps(block, sort_keys=True).encode()
        return hashlib.sha256(block_encoded).hexdigest()

    def __init__(self):
        self.nodes = set()
        self.chain=[]
        self.current_transactions=[]
        genesis_hash=self.hash_block("genesis_block")
        self.append_block(
            hash_of_previous_block=genesis_hash,
            nonce=self.proof_of_work(0, genesis_hash, []))
    
    def proof_of_work(self, index, hash_of_previous_block, transactions):
        nonce=0
        while self.valid_proof(index, hash_of_previous_block, transactions, nonce) is False:
            nonce += 1

        return nonce

    def valid_proof(self, index, hash_of_previous_block, transactions,nonce):
        content=f'{index}{hash_of_previous_block}{transactions}{nonce}'.encode()
        content_hash= hashlib.sha256(content).hexdigest()
        return content_hash[:len(self.difficulty_target)]== self.difficulty_target

    def append_block(self, nonce, hash_of_previous_block):
        block={ 'index': len(self.chain),
                'timestamp': time(),
                'transactions': self.current_transactions,
                'nonce': nonce,
                'hash_of_previous_block': hash_of_previous_block
                }

        self.current_transactions =[]
        self.chain.append(block)
        return block
        
    def add_transactions(self, sender, recipient, amount):
        self.current_transactions.append({
            'amount': amount,
            'recipient': recipient,
            'sender': sender,
        })
        return self.last_block['index'] + 1

    @property
    def last_block(self):
        return self.chain[-1]

    def add_node(self, address):
        parsed_url = urlparse(address)
        self.nodes.add(parsed_url.netloc)
        print(parsed_url.netloc)

    # determine if a given blockchain is valid
    def valid_chain(self, chain):
        last_block = chain[0] # the genesis block
        current_index = 1 # starts with the second block
        while current_index < len(chain):
            # get the current block
            block = chain[current_index]
            # check that the hash of the previous block is
            # correct by hashing the previous block and then
            # comparing it with the one recorded in the
            # current block
            if block['hash_of_previous_block'] != self.hash_block(last_block):
                return False
            # check that the nonce is correct by hashing the
            # hash of the previous block together with the
            # nonce and see if it matches the target
            if not self.valid_proof(
                current_index,
                block['hash_of_previous_block'],
                block['transactions'],
                block['nonce']):
                return False
            # move on to the next block on the chain
            last_block = block
            current_index += 1
        # the chain is valid
        return True
    def update_blockchain(self):
        # get the nodes around us that has been registered
        neighbours = self.nodes
        new_chain = None
        # for simplicity, look for chains longer than ours
        max_length = len(self.chain)
        # grab and verify the chains from all the nodes in
        # our network
        for node in neighbours:
            # get the blockchain from the other nodes
            response = requests.get(f'http://{node}/blockchain')
            if response.status_code == 200:
                length = response.json()['length']
                chain = response.json()['chain']
            # check if the length is longer and the chain
            # is valid
            if length > max_length and self.valid_chain(chain):
                max_length = length
                new_chain = chain
        # replace our chain if we discovered a new, valid
        # chain longer than ours
        if new_chain:
            self.chain = new_chain
            return True
        return False

app = Flask(__name__)
node_identifier= str(uuid4()).replace('-', '')
blockchain= Blockchain()

@app.route('/blockchain', methods=['GET'])
def full_chain():
    response= { 'chain': blockchain.chain,
                'length': len(blockchain.chain),
                }
    return jsonify(response), 200

@app.route('/mine', methods=['GET'])
def mine_block():
    start_time = time()

    blockchain.add_transactions(sender="0", recipient=node_identifier, amount=1)
    last_block_hash = blockchain.hash_block(blockchain.last_block)
    index = len(blockchain.chain)
    nonce = blockchain.proof_of_work(index, last_block_hash, blockchain.current_transactions)
    block = blockchain.append_block(nonce, last_block_hash)

    mining_duration = round(time() - start_time, 4)

    response = {
        'message': "New Block Mined",
        'index': block['index'],
        'hash_of_previous_block': block['hash_of_previous_block'],
        'nonce': block['nonce'],
        'transactions': block['transactions'],
        'mining_time_seconds': mining_duration
    }

    return jsonify(response), 200


@app.route('/transactions/new', methods=['POST'])
def new_transaction():
    start_time = time()
    values = request.get_json()
    required_fields = ['sender', 'recipient', 'amount']
    if not all(k in values for k in required_fields):
        return ('Missing fields', 400)

    index = blockchain.add_transactions(values['sender'], values['recipient'], values['amount'])
    duration = round(time() - start_time, 4)

    response = {
        'message': f'Transaction will be added to block {index}',
        'transaction_add_time_seconds': duration
    }
    return (jsonify(response), 201)


@app.route('/nodes/add_nodes', methods=['POST'])
def add_nodes():
    # get the nodes passed in from the client
    values = request.get_json()
    nodes = values.get('nodes')
    if nodes is None:
        return "Error: Missing node(s) info", 400
    for node in nodes:
        blockchain.add_node(node)
    response = {
        'message': 'New nodes added',
        'nodes': list(blockchain.nodes),
    }
    return jsonify(response), 201

@app.route('/nodes/sync', methods=['GET'])
def sync():
    start_time = time()
    updated = blockchain.update_blockchain()
    sync_time = round(time() - start_time, 4)

    if updated:
        response = {
            'message': 'The blockchain has been updated to the latest',
            'blockchain': blockchain.chain,
            'sync_time_seconds': sync_time
        }
    else:
        response = {
            'message': 'Our blockchain is the latest',
            'blockchain': blockchain.chain,
            'sync_time_seconds': sync_time
        }
    return jsonify(response), 200


if __name__=='__main__':
    #app.run(host='0.0.0.0', port=int(sys.argv[1]))
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 5000
    app.run(host='0.0.0.0', port=port)



